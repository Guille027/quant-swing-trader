"""Telegram Bot API client (stdlib only).

Bot API conventions used here (as implemented by python-telegram-bot; core.telegram.org is not reachable from the
build environment, so this is UNVERIFIED against the live service until the first real message is sent):
- methods are POSTed to https://api.telegram.org/bot<token>/<method>;
- every response is JSON {"ok": bool, "result": ..., "description": str, "error_code": int,
  "parameters": {"retry_after": seconds}} (errors also come with an HTTP 4xx status);
- a text message has at most 4096 characters; about 1 message per second per chat;
- a bot can only write to a user who has opened it and pressed Start (or sent it a message) first;
- getUpdates returns the messages sent to the bot, each with message.chat.id.
The token is a secret: it is never logged and is scrubbed from error messages.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096


class TelegramError(RuntimeError):
    pass


def _http_post(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:  # Telegram explains the error in the JSON body
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"ok": False, "error_code": e.code, "description": f"HTTP {e.code}"}


def split_message(text: str, limit: int = MAX_LEN - 96) -> list[str]:
    """Split at line breaks so no part exceeds the limit (a single over-long line is cut)."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [""]


class Telegram:
    def __init__(self, token: str, chat_id: str | int | None = None,
                 http: Callable[[str, dict, float], dict] | None = None, timeout: float = 20.0,
                 pause: float = 1.1):
        if not token or ":" not in token:
            raise TelegramError("el token no tiene el formato de BotFather (números:letras)")
        self._token, self.chat_id = token.strip(), chat_id
        self._http, self.timeout, self.pause = http or _http_post, timeout, pause

    def _scrub(self, msg: str) -> str:
        return str(msg).replace(self._token, "***")

    def call(self, method: str, payload: dict | None = None) -> object:
        url = API.format(token=self._token, method=method)
        try:
            r = self._http(url, payload or {}, self.timeout)
        except Exception as e:  # noqa: BLE001 - network: no internet, proxy, timeout...
            raise TelegramError(f"no se pudo contactar con Telegram: {self._scrub(repr(e))}") from None
        if not r.get("ok"):
            wait = (r.get("parameters") or {}).get("retry_after")
            desc = r.get("description") or f"error {r.get('error_code')}"
            raise TelegramError(self._scrub(desc) + (f" (reintentar en {wait} s)" if wait else ""))
        return r.get("result")

    def me(self) -> dict:
        """The bot itself (checks the token)."""
        return self.call("getMe")

    def find_chat(self) -> dict | None:
        """Chat of the latest message sent to the bot (the user must open the bot and press Start first)."""
        updates = self.call("getUpdates", {"timeout": 0}) or []
        for u in reversed(updates):
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("title")
                return {"id": chat["id"], "name": name or chat.get("username") or str(chat["id"])}
        return None

    def send(self, text: str, *, html: bool = True) -> int:
        """Sends `text` (split if too long). Returns the number of messages sent."""
        if self.chat_id in (None, ""):
            raise TelegramError("falta el chat: abre tu bot en Telegram, pulsa Iniciar y luego 'Detectar mi chat'")
        parts = split_message(text)
        for i, part in enumerate(parts):
            if i:
                time.sleep(self.pause)  # about 1 message per second per chat
            payload = {"chat_id": self.chat_id, "text": part}
            if html:
                payload["parse_mode"] = "HTML"
            self.call("sendMessage", payload)
        return len(parts)
