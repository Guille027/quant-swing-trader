"""Evening report of the paper-trading session to Telegram (runs inside the app while it is open).

After each NYSE close (+ a delay so the vendor publishes the day's bar) it updates the session's prices, recomputes
the simulation and sends ONE daily message (plus a sell alert when something has to be sold or was closed that
day). If the app was closed at that time, the message for the latest close is sent as soon as the app is opened
again: the orders are for the next open (15:30 Spanish time), so opening the app any time before that is enough.
A day is never notified twice (PaperNotification), and nothing is sent without an active simulation.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable

import pandas as pd
from sqlalchemy import select

from qsts.data.bars import nyse_schedule
from qsts.db import models as m
from qsts.notify.report import daily_messages
from qsts.notify.telegram import TelegramError

MAX_UPDATE_ATTEMPTS = 3
RETRY_MINUTES = 20


def due_session(now: pd.Timestamp, delay_min: int) -> pd.Timestamp:
    """Latest session whose close (+ delay) has passed."""
    sched = nyse_schedule(now - pd.Timedelta(days=14), now)
    ready = sched.index[sched["market_close"] + pd.Timedelta(minutes=delay_min) <= now]
    return ready[-1]


class DailyReporter:
    def __init__(self, sf, paper: Callable[[], object], telegram: Callable[[], object | None],
                 data_runner: Callable[[], object] | None = None, last_bar: Callable[[str], pd.Timestamp | None] | None = None,
                 benchmark: str = "SPY", delay_min: int = 45, poll_seconds: float = 120.0,
                 log: Callable[[str], None] | None = None, blocked: Callable[[], str | None] | None = None,
                 intraday_paper: Callable[[], object] | None = None,
                 intraday_data: Callable[[], dict] | None = None,
                 intraday_update: Callable[[str, list[str]], bool] | None = None):
        """`blocked()`: a reason not to send now (e.g. newer data from another computer waits to be loaded).
        Intraday (optional): `intraday_data()` -> {dataset: (symbols with bars, newest bar)}; `intraday_update(dataset,
        symbols)` starts their download; `intraday_paper()` -> the intraday paper simulation."""
        self.sf, self.paper, self.telegram = sf, paper, telegram
        self.intraday_paper, self.intraday_data, self.intraday_update = intraday_paper, intraday_data, intraday_update
        self.intraday_state = "se comprueba en unos segundos"
        self._intraday_fresh: dict = {}  # "*" -> latest session the stored intraday bars are known to cover
        self.data_runner, self.last_bar, self.benchmark = data_runner, last_bar, benchmark
        self.delay_min, self.poll, self.blocked = delay_min, poll_seconds, blocked
        self.logs: deque[str] = deque(maxlen=50)
        self.log = log or (lambda msg: self.logs.append(f"{datetime.now().strftime('%d/%m %H:%M')}  {msg}"))
        self.state = "parado"
        self._attempts: dict = {}  # due day -> (attempts, last attempt time)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ records
    def sent(self, session_id: int, day, kind: str = "daily") -> bool:
        with self.sf() as s:
            return s.scalars(select(m.PaperNotification.id).where(
                m.PaperNotification.session_id == session_id, m.PaperNotification.day == day,
                m.PaperNotification.kind == kind, m.PaperNotification.ok.is_(True))).first() is not None

    def last_failure_age(self, session_id: int, day) -> float | None:
        with self.sf() as s:
            r = s.scalars(select(m.PaperNotification).where(
                m.PaperNotification.session_id == session_id, m.PaperNotification.day == day,
                m.PaperNotification.ok.is_(False)).order_by(m.PaperNotification.sent_at.desc())).first()
        if r is None:
            return None
        return (datetime.now(timezone.utc).replace(tzinfo=None) - r.sent_at).total_seconds() / 60

    def _record(self, session_id: int, day, kind: str, ok: bool, text: str, error: str | None = None) -> None:
        with self.sf() as s, s.begin():
            s.add(m.PaperNotification(session_id=session_id, day=day, kind=kind, ok=ok, text=text[:20000], error=error))

    # ------------------------------------------------------------------ sending
    def send_report(self, kind: str = "manual", day=None) -> dict:
        """Recompute the simulation and send its messages now (`day`: the close it is recorded for)."""
        tg = self.telegram()
        if tg is None:
            raise TelegramError("Telegram no está configurado")
        p = self.paper()
        ps = p.active()
        if ps is None:
            raise TelegramError("no hay ninguna simulación en marcha")
        v = p.view()
        msgs = daily_messages(v)
        day = day or pd.Timestamp(v["as_of"]).date()
        text = "\n\n".join(msgs)
        try:
            for msg in msgs:
                tg.send(msg)
        except TelegramError as e:
            self._record(ps.id, day, kind, False, text, str(e))
            raise
        self._record(ps.id, day, kind, True, text)
        return {"day": str(day), "messages": len(msgs)}

    def tick(self, now: pd.Timestamp | None = None) -> str:
        now = now or pd.Timestamp.now(tz="UTC")
        p = self.paper()
        ps = p.active()
        if ps is None:
            return self._set("sin simulación en marcha: no hay nada que avisar")
        if self.telegram() is None:
            return self._set("Telegram no configurado")
        why = self.blocked() if self.blocked is not None else None
        if why:
            return self._set(f"en espera: {why}")
        due = due_session(now, self.delay_min)
        day = due.date()
        if self.sent(ps.id, day):
            return self._set(f"aviso del {day} ya enviado")
        age = self.last_failure_age(ps.id, day)
        if age is not None and age < RETRY_MINUTES:
            return self._set(f"el último envío falló; se reintenta en {RETRY_MINUTES - age:.0f} min")
        if self.last_bar is not None:  # prices first: the message must be about the latest close
            have = max((b for b in (self.last_bar(s) for s in ps.symbols) if b is not None), default=None)
            if have is None or have < due:
                runner = self.data_runner() if self.data_runner else None
                n, last = self._attempts.get(day, (0, None))
                if runner is not None and runner.state.running:
                    return self._set("esperando a que termine la descarga de precios")
                if runner is not None and n < MAX_UPDATE_ATTEMPTS and (last is None or now - last >= pd.Timedelta(minutes=RETRY_MINUTES)):
                    if runner.start("simulación", sorted(set(ps.symbols) | {self.benchmark}), incremental=True):
                        self._attempts[day] = (n + 1, now)
                        self.log(f"Descargando los precios del {day} para el aviso diario")
                    return self._set(f"descargando los precios del {day}")
                if n < MAX_UPDATE_ATTEMPTS:
                    return self._set(f"esperando los precios del {day}")
                # the vendor still has not published the day: send with a stale warning rather than nothing
        try:
            r = self.send_report("daily", day)
        except TelegramError as e:
            self.log(f"Aviso de Telegram NO enviado: {e}")
            return self._set(f"error al enviar: {e}")
        self.log(f"Aviso diario del {r['day']} enviado por Telegram ({r['messages']} mensaje(s))")
        return self._set(f"aviso del {r['day']} enviado")

    def _set(self, state: str) -> str:
        self.state = state
        return state

    # ------------------------------------------------------------------ background thread
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="daily-report")
        self._thread.start()

    def _loop(self) -> None:
        self._stop.wait(15)  # let the app finish starting
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the reporter must never take the app down
                self._set(f"error: {e!r}"[:200])
                self.log(f"Aviso diario: error {e!r}"[:300])
            try:
                self.intraday_tick()
            except Exception as e:  # noqa: BLE001
                self.intraday_state = f"error: {e!r}"[:200]
                self.log(f"Intradía: error {e!r}"[:300])
            self._stop.wait(self.poll)

    # ------------------------------------------------------------------ intraday bars + intraday simulation
    def intraday_tick(self, now: pd.Timestamp | None = None) -> str:
        """Keep the stored intraday bars up to date (Yahoo keeps 5-minute bars only 60 days: downloading them every
        day while the app is open is what lets the history grow), then record the intraday simulation's new
        sessions and send each one by Telegram."""
        if self.intraday_data is None:
            return self.intraday_state
        from qsts.research.intraday import session_dates
        now = now or pd.Timestamp.now(tz="UTC")
        due = due_session(now, self.delay_min)
        if self._intraday_fresh.get("*") != due:  # bars not yet known to cover the latest session
            runner = self.data_runner() if self.data_runner else None
            waiting = False
            for ds, (symbols, newest) in sorted((self.intraday_data() or {}).items()):
                have = session_dates(pd.DatetimeIndex([newest]))[0] if newest is not None else None
                if not symbols or (have is not None and have >= due):
                    continue
                key = ("intraday", ds, due.date())
                n, last = self._attempts.get(key, (0, None))
                if n >= MAX_UPDATE_ATTEMPTS:
                    continue  # Yahoo has nothing newer for this session: try again with the next one
                if runner is not None and runner.state.running:
                    self.intraday_state = "esperando a que termine otra descarga"
                    return self.intraday_state
                if last is not None and now - last < pd.Timedelta(minutes=RETRY_MINUTES):
                    waiting = True
                    continue
                if self.intraday_update is not None and self.intraday_update(ds, list(symbols)):
                    self._attempts[key] = (n + 1, now)
                    self.log(f"Descargando las velas de {ds} del {due.date()} ({len(symbols)} acciones)")
                    self.intraday_state = f"descargando las velas de {ds} del {due.date()}"
                    return self.intraday_state
                waiting = True
            if not waiting:
                self._intraday_fresh["*"] = due
        ip = self.intraday_paper() if self.intraday_paper else None
        if ip is None or ip.active() is None:
            self.intraday_state = "velas al día" if self._intraday_fresh.get("*") == due else "actualizando velas"
            return self.intraday_state
        ip.update()
        view = ip.view(refresh=False)
        ps = ip.active()
        pending = [d for d in ip.journal(ps.id) if d.notified_at is None]
        tg = self.telegram()
        why = self.blocked() if self.blocked is not None else None
        if not pending or tg is None or why:
            self.intraday_state = ("simulación al día" if not pending else
                                   "Telegram no configurado" if tg is None else f"en espera: {why}")
            return self.intraday_state
        from qsts.notify.report import intraday_day_message
        for d in pending[:-3]:  # older sessions (app closed for days) are in the app; only the last 3 are sent
            ip.mark_notified(d.id)
        for d in pending[-3:]:
            try:
                tg.send(intraday_day_message(view, {"day": str(d.day), "equity": d.equity, "pnl": d.pnl,
                                                    "passive": d.passive, "trades": d.trades}))
            except TelegramError as e:
                self.log(f"Intradía: aviso NO enviado: {e}")
                self.intraday_state = f"error al enviar: {e}"
                return self.intraday_state
            ip.mark_notified(d.id)
            self.log(f"Intradía: resultado del {d.day} enviado por Telegram")
        self.intraday_state = "simulación al día; aviso enviado"
        return self.intraday_state

    def stop(self) -> None:
        self._stop.set()
