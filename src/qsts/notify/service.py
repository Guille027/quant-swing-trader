"""NotificationService with pluggable channels.

Status per channel:
- LogChannel:       IMPLEMENTED
- TelegramChannel:  UNVERIFIED (Bot API sendMessage; endpoint not reachable from build env)
- DiscordChannel:   UNVERIFIED (incoming webhook JSON {"content": ...})
- EmailChannel:     IMPLEMENTED (stdlib SMTP; tested with a fake SMTP class)
- WhatsAppChannel:  PLACEHOLDER -- requires choosing a provider (Meta WhatsApp Cloud API or Twilio) and
                    reading its current docs; it raises NotImplementedError rather than pretending.
A failing channel never blocks trading logic or other channels; failures are recorded.
"""
from __future__ import annotations

import json
import smtplib
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from enum import Enum

from qsts.core.logging import get_logger

log = get_logger("notify")


class Event(str, Enum):
    NEW_SIGNAL = "NEW_SIGNAL"
    TRADE_APPROVED = "TRADE_APPROVED"
    TRADE_EXECUTED = "TRADE_EXECUTED"
    TRADE_CLOSED = "TRADE_CLOSED"
    CRITICAL_ERROR = "CRITICAL_ERROR"
    CONNECTION_LOST = "CONNECTION_LOST"
    CONNECTION_RESTORED = "CONNECTION_RESTORED"
    STRATEGY_DEGRADED = "STRATEGY_DEGRADED"
    EXCESSIVE_DRAWDOWN = "EXCESSIVE_DRAWDOWN"
    BROKER_ERROR = "BROKER_ERROR"
    KILL_SWITCH = "KILL_SWITCH"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"


CRITICAL = {Event.CRITICAL_ERROR, Event.CONNECTION_LOST, Event.BROKER_ERROR, Event.KILL_SWITCH,
            Event.EXCESSIVE_DRAWDOWN, Event.RECONCILIATION_MISMATCH}


@dataclass
class Notification:
    event: Event
    title: str
    body: str = ""
    data: dict = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def text(self) -> str:
        return f"[{self.event.value}] {self.title}\n{self.body}".strip()


class Channel(ABC):
    name: str

    @abstractmethod
    def send(self, n: Notification) -> None: ...


class LogChannel(Channel):
    name = "log"

    def __init__(self):
        self.sent: list[Notification] = []

    def send(self, n):
        self.sent.append(n)
        (log.warning if n.event in CRITICAL else log.info)(n.text())


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status >= 300:
            raise RuntimeError(f"HTTP {r.status}")


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self._token, self.chat_id = bot_token, chat_id

    def send(self, n):
        _post_json(f"https://api.telegram.org/bot{self._token}/sendMessage", {"chat_id": self.chat_id, "text": n.text()})


class DiscordChannel(Channel):
    name = "discord"

    def __init__(self, webhook_url: str):
        self._url = webhook_url

    def send(self, n):
        _post_json(self._url, {"content": n.text()[:1900]})


class EmailChannel(Channel):
    name = "email"

    def __init__(self, host: str, port: int, user: str, password: str, sender: str, to: str, smtp_cls=smtplib.SMTP_SSL):
        self.host, self.port, self.user, self._pw, self.sender, self.to = host, port, user, password, sender, to
        self.smtp_cls = smtp_cls

    def send(self, n):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = f"QSTS {n.event.value}: {n.title}", self.sender, self.to
        msg.set_content(n.text())
        with self.smtp_cls(self.host, self.port) as s:
            s.login(self.user, self._pw)
            s.send_message(msg)


class WhatsAppChannel(Channel):
    name = "whatsapp"

    def send(self, n):
        raise NotImplementedError("WhatsApp provider not selected/verified yet (PLACEHOLDER)")


class NotificationService:
    def __init__(self, channels: list[Channel] | None = None, muted: set[Event] | None = None):
        self.channels = channels or [LogChannel()]
        self.muted = muted or set()
        self.failures: list[tuple[str, str, str]] = []

    def notify(self, event: Event, title: str, body: str = "", **data) -> Notification:
        n = Notification(event, title, body, data)
        if event in self.muted and event not in CRITICAL:  # critical events can never be muted
            return n
        for ch in self.channels:
            try:
                ch.send(n)
            except Exception as e:  # noqa: BLE001 - a channel failure must never propagate into trading
                self.failures.append((ch.name, event.value, repr(e)))
                log.error("notification via %s failed: %r", ch.name, e)
        return n
