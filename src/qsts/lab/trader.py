"""Automatic paper trading of the activated bots on the Alpaca PAPER account (simulated money only).

Same rules as the backtest, in real time: after each session's close (+ a delay so the day's bar is published)
the app downloads the prices, computes each active bot's signal on that close and sends the orders, which Alpaca
queues for the next open (a `day` order sent after the close is executed the next trading day). Stops and targets
are resting orders at Alpaca, so they work while this computer is off; every evening they are checked and re-sent
if missing. Fills are read back from Alpaca and reported by Telegram.

Honest differences from the backtest (shown in the app): whole shares only; stop/target levels from a percentage
are computed on the signal day's close (the fill price is not known yet); a reversal closes first and opens the
other side at the following open; if the app was not running between the close and the next open, that day's
entries are skipped (never sent late) while exits are still sent.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import select

from qsts.broker.alpaca_paper import BrokerError
from qsts.core.hashing import hash_obj
from qsts.data.bars import nyse_schedule
from qsts.db import models as m
from qsts.lab.strategy import REGISTRY

FINAL = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day", "stopped", "suspended"}
OPEN_ORDER = {"new", "accepted", "pending_new", "accepted_for_bidding", "held", "partially_filled", "calculated",
              "pending_replace", "pending_cancel", "submitted"}
MAX_UPDATE_ATTEMPTS, RETRY_MINUTES = 3, 20
PURPOSE = {"entry": "entrada", "exit": "salida", "stop": "stop", "target": "objetivo", "protect": "protección"}


def due_session(now: pd.Timestamp, delay_min: int) -> pd.Timestamp:
    """Latest session whose close (+ delay) has passed."""
    sched = nyse_schedule(now - pd.Timedelta(days=14), now)
    ready = sched.index[sched["market_close"] + pd.Timedelta(minutes=delay_min) <= now]
    return ready[-1]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def money(x: float | None) -> str:
    if x is None:
        return "—"
    s = f"{abs(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if x < 0 else "") + s + " $"


class PaperTrader:
    def __init__(self, sf, lab, broker: Callable[[], object | None], telegram: Callable[[], object | None] | None = None,
                 refresh: Callable[[list[str]], bool] | None = None, data_busy: Callable[[], bool] | None = None,
                 delay_min: int = 45, blocked: Callable[[], str | None] | None = None):
        self.sf, self.lab, self.broker, self.telegram = sf, lab, broker, telegram
        self.refresh, self.data_busy, self.delay_min, self.blocked = refresh, data_busy, delay_min, blocked
        self.state = "parado"
        self.logs: deque[str] = deque(maxlen=100)
        self._attempts: dict = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ records
    def event(self, kind: str, text: str, bot_id: str | None = None, notify: bool = False) -> None:
        with self.sf() as s, s.begin():
            s.add(m.LabEvent(bot_id=bot_id, kind=kind, text=text))
        self.logs.append(f"{datetime.now().strftime('%d/%m %H:%M')}  {text}")
        if notify:
            self.notify(text)

    def notify(self, text: str) -> None:
        tg = self.telegram() if self.telegram else None
        if tg is None or (self.blocked and self.blocked()):
            return
        try:
            tg.send(text)
        except Exception as e:  # noqa: BLE001 - a message that fails never stops trading
            self.logs.append(f"{datetime.now().strftime('%d/%m %H:%M')}  Telegram no enviado: {e}")

    def events(self, bot_id: str | None = None, limit: int = 100) -> list[dict]:
        with self.sf() as s:
            q = select(m.LabEvent).order_by(m.LabEvent.id.desc()).limit(limit)
            if bot_id:
                q = q.where(m.LabEvent.bot_id == bot_id)
            return [{"at": e.at.isoformat(timespec="minutes"), "bot": e.bot_id, "kind": e.kind, "text": e.text}
                    for e in s.scalars(q)]

    def orders(self, bot_id: str | None = None) -> list[m.LabOrder]:
        with self.sf() as s:
            q = select(m.LabOrder).order_by(m.LabOrder.submitted_at)
            if bot_id:
                q = q.where(m.LabOrder.bot_id == bot_id)
            return list(s.scalars(q))

    def active_bots(self) -> list[m.LabBot]:
        with self.sf() as s:
            return [b for b in s.scalars(select(m.LabBot).where(m.LabBot.paper_status == "active"))
                    if b.strategy in REGISTRY]

    def _update_bot(self, bid: str, **fields) -> None:
        with self.sf() as s, s.begin():
            b = s.get(m.LabBot, bid)
            for k, v in fields.items():
                setattr(b, k, v)

    # ------------------------------------------------------------------ ledger of one bot (from its fills)
    def ledger(self, b: m.LabBot) -> dict:
        """Position, average price, realised P&L and closed trades, replayed from the bot's filled orders."""
        qty = avg = realized = 0.0
        trades, opened = [], None
        fills = sorted((o for o in self.orders(b.id) if (o.filled_qty or 0) > 0 and o.filled_price),
                       key=lambda o: o.filled_at or o.submitted_at)
        for o in fills:
            q = o.filled_qty if o.side == "buy" else -o.filled_qty
            if qty == 0 or np.sign(q) == np.sign(qty):  # opening / adding
                avg = (avg * abs(qty) + o.filled_price * abs(q)) / (abs(qty) + abs(q))
                if qty == 0:
                    opened = o
                qty += q
            else:  # reducing / closing
                closed = min(abs(q), abs(qty))
                pnl = closed * (o.filled_price - avg) * np.sign(qty)
                realized += pnl
                trades.append({"side": "largo" if qty > 0 else "corto", "entry": (opened.filled_at or opened.submitted_at).date().isoformat() if opened else None,
                               "exit": (o.filled_at or o.submitted_at).date().isoformat(), "entry_price": avg,
                               "exit_price": o.filled_price, "qty": closed, "pnl": pnl,
                               "pnl_pct": pnl / (closed * avg) if avg else None, "reason": PURPOSE.get(o.purpose, o.purpose)})
                qty += q
                if abs(qty) < 1e-9:
                    qty, avg, opened = 0.0, 0.0, None
        return {"qty": qty, "avg": avg, "realized": realized, "trades": trades,
                "equity": (b.capital or 0.0) + realized}

    def live_curve(self, b: m.LabBot, bars: pd.DataFrame) -> pd.Series:
        """The bot's paper equity at each close since it was activated (fills replayed day by day)."""
        if not b.activated_at:
            return pd.Series(dtype=float)
        start = pd.Timestamp(b.activated_at).tz_localize("UTC").normalize()
        closes = bars["close"][bars.index >= start]
        if not len(closes):
            return pd.Series(dtype=float)
        fills = sorted((o for o in self.orders(b.id) if (o.filled_qty or 0) > 0 and o.filled_price),
                       key=lambda o: o.filled_at or o.submitted_at)
        cash, qty, k, out = b.capital or 0.0, 0.0, 0, []
        for day, close in closes.items():
            end = day + pd.Timedelta(days=1)
            while k < len(fills) and pd.Timestamp(fills[k].filled_at or fills[k].submitted_at).tz_localize("UTC") < end:
                o = fills[k]
                q = o.filled_qty if o.side == "buy" else -o.filled_qty
                cash -= q * o.filled_price
                qty += q
                k += 1
            out.append(cash + qty * close)
        return pd.Series(out, index=closes.index)

    # ------------------------------------------------------------------ activation
    def activate(self, bid: str, allocation_pct: float, follow_open: bool = True, now: pd.Timestamp | None = None) -> dict:
        with self._lock:
            b = self.lab.get_bot(bid)
            if b.paper_status == "active":
                raise ValueError("este bot ya está en paper trading")
            if not 1 <= allocation_pct <= 100:
                raise ValueError("la parte de la cuenta debe estar entre 1 y 100%")
            broker = self.broker()
            if broker is None:
                raise ValueError("conecta primero tu cuenta paper de Alpaca (Ajustes)")
            others = [x for x in self.active_bots() if x.symbol == b.symbol]
            if others:
                raise ValueError(f"ya hay un bot activo con {b.symbol} ({others[0].id}): dos bots en la misma acción "
                                 "se mezclarían en la misma posición de Alpaca")
            used = sum(x.allocation_pct or 0 for x in self.active_bots())
            if used + allocation_pct > 100:
                raise ValueError(f"ya tienes asignado el {used:g}% de la cuenta: no se puede pasar del 100% (sin apalancamiento)")
            acct = broker.account()
            if acct.get("trading_blocked") or acct.get("account_blocked"):
                raise ValueError("Alpaca indica que la cuenta tiene el trading bloqueado")
            capital = float(acct["equity"]) * allocation_pct / 100
            pending = None
            if follow_open:  # the backtest is inside a trade right now: open the same position at the next open
                res, _bars = self.lab.result(b)
                ot = res.open_trade
                if ot is not None:
                    pending = {"direction": int(ot["direction"]), "reason": "seguir la operación abierta del backtest",
                               "stop": ot.get("stop"), "target": ot.get("target"), "created": _now().isoformat()}
            at = (now or pd.Timestamp.now(tz="UTC")).tz_convert("UTC").tz_localize(None).to_pydatetime()
            self._update_bot(bid, paper_status="active", allocation_pct=allocation_pct, capital=capital,
                             activated_at=at, stopped_at=None, last_signal_day=None, stop_level=None,
                             target_level=None, pending=pending)
            st = REGISTRY[b.strategy]
            self.event("info", f"▶️ Bot activado en paper: «{st.name}» con {b.symbol}, {allocation_pct:g}% de la cuenta "
                               f"({money(capital)})" + (" — entrará en la próxima apertura como el backtest" if pending else ""),
                       bid, notify=True)
            return {"capital": capital, "follow": pending is not None}

    def deactivate(self, bid: str, close: bool = True) -> dict:
        with self._lock:
            b = self.lab.get_bot(bid)
            if b.paper_status != "active":
                raise ValueError("este bot no está en paper trading")
            broker = self.broker()
            closed = False
            if broker is not None:
                self._cancel_open(broker, b)
                led = self.ledger(b)
                if close and led["qty"]:
                    self._submit(broker, b, "sell" if led["qty"] > 0 else "buy", abs(led["qty"]), "exit", None)
                    closed = True
            self._update_bot(bid, paper_status="stopped", stopped_at=_now(), pending=None)
            self.event("info", f"⏹️ Bot detenido: {bid}" + (" (se cierra su posición en la próxima apertura)" if closed else ""),
                       bid, notify=True)
            return {"closing": closed}

    # ------------------------------------------------------------------ orders
    def _client_id(self, b: m.LabBot, purpose: str) -> str:
        return f"qsts-{hash_obj([b.id, purpose, _now().isoformat()], 10)}-{purpose[:3]}"

    def _submit(self, broker, b: m.LabBot, side: str, qty: float, purpose: str, signal_day,
                stop: float | None = None, target: float | None = None, order_type: str = "market",
                tif: str = "day") -> m.LabOrder | None:
        cid = self._client_id(b, purpose)
        req = {"symbol": b.symbol, "qty": int(qty), "side": side, "type": order_type, "time_in_force": tif,
               "client_order_id": cid}
        kind = order_type
        if purpose == "entry" and (stop or target):
            if stop and target:
                req.update(order_class="bracket", stop_loss=stop, take_profit=target)
                kind = "bracket"
            else:
                req.update(order_class="oto", **({"stop_loss": stop} if stop else {"take_profit": target}))
                kind = "oto"
        elif purpose == "protect":
            if stop and target:
                req.update(type="limit", limit_price=target, order_class="oco", stop_loss=stop, take_profit=target)
                kind = "oco"
            elif stop:
                req.update(type="stop", stop_price=stop)
                kind = "stop"
            else:
                req.update(type="limit", limit_price=target)
                kind = "limit"
        row = m.LabOrder(id=cid, bot_id=b.id, symbol=b.symbol, side=side, qty=int(qty), purpose=purpose,
                         order_type=kind, stop_price=stop, limit_price=target, signal_day=signal_day)
        try:
            o = broker.submit(req)
        except BrokerError as e:
            row.status, row.error = "rejected", str(e)[:500]
            with self.sf() as s, s.begin():
                s.add(row)
            self.event("error", f"⚠️ Alpaca rechazó la orden de {PURPOSE.get(purpose, purpose)} de {b.symbol} ({b.id}): {e}",
                       b.id, notify=True)
            return None
        row.broker_id, row.status, row.raw = o.get("id"), o.get("status") or "submitted", o
        with self.sf() as s, s.begin():
            s.add(row)
        return row

    def _cancel_open(self, broker, b: m.LabBot, purposes: tuple = ("entry", "exit", "protect", "stop", "target")) -> int:
        n = 0
        for o in self.orders(b.id):
            if o.purpose in purposes and o.status in OPEN_ORDER and o.broker_id:
                try:
                    broker.cancel(o.broker_id)
                    n += 1
                except BrokerError as e:
                    self.event("error", f"No se pudo cancelar una orden de {b.symbol}: {e}", b.id)
        return n

    def sync_orders(self, broker) -> int:
        """Read back every unfinished order (and its stop / target legs): record fills and report them."""
        new_fills = 0
        for o in self.orders():
            if o.status in FINAL or not o.broker_id:
                continue
            try:
                bo = broker.get_order(o.broker_id)
            except BrokerError as e:
                self.state = f"no se puede consultar Alpaca: {e}"
                return new_fills
            new_fills += self._record(o, bo)
            for leg in bo.get("legs") or []:
                lid = f"{o.id}-{str(leg.get('id'))[:8]}"
                with self.sf() as s:
                    have = s.get(m.LabOrder, lid)
                if have is None:
                    purpose = "stop" if leg.get("type") in ("stop", "stop_limit") else "target"
                    have = m.LabOrder(id=lid, bot_id=o.bot_id, broker_id=leg.get("id"), symbol=o.symbol,
                                      side=leg.get("side") or ("sell" if o.side == "buy" else "buy"),
                                      qty=leg.get("qty") or o.qty, purpose=purpose, order_type=leg.get("type") or "",
                                      stop_price=leg.get("stop_price"), limit_price=leg.get("limit_price"),
                                      status="submitted")
                    with self.sf() as s, s.begin():
                        s.add(have)
                new_fills += self._record(have, leg)
        return new_fills

    def _record(self, row: m.LabOrder, bo: dict) -> int:
        status = bo.get("status") or row.status
        filled_qty, price = bo.get("filled_qty"), bo.get("filled_avg_price")
        newly = (filled_qty or 0) > 0 and not row.notified and status in FINAL
        with self.sf() as s, s.begin():
            r = s.get(m.LabOrder, row.id)
            r.status, r.filled_qty, r.filled_price = status, filled_qty, price
            r.filled_at = pd.Timestamp(bo["filled_at"]).tz_convert("UTC").tz_localize(None).to_pydatetime() \
                if bo.get("filled_at") else r.filled_at
            if newly:
                r.notified = True
        if not newly:
            if status in ("rejected", "expired", "canceled") and not row.notified and (filled_qty or 0) == 0 \
                    and row.purpose in ("entry", "exit"):
                with self.sf() as s, s.begin():
                    s.get(m.LabOrder, row.id).notified = True
                self.event("skip", f"⚠️ La orden de {PURPOSE[row.purpose]} de {row.symbol} quedó «{status}» sin ejecutarse "
                                   f"({row.bot_id}).", row.bot_id, notify=True)
            return 0
        b = self.lab.get_bot(row.bot_id)
        verb = {"buy": "compradas", "sell": "vendidas"}[row.side]
        icon = {"stop": "🛑 Stop ejecutado", "target": "🎯 Objetivo alcanzado"}.get(row.purpose, "✅ Ejecutada")
        text = f"{icon}: {verb} {filled_qty:g} {row.symbol} a {money(price)} — bot «{REGISTRY[b.strategy].name}»"
        if row.purpose in ("exit", "stop", "target"):
            led = self.ledger(b)
            if led["trades"]:
                t = led["trades"][-1]
                pct = f"{t['pnl_pct'] * 100:+.2f}".replace(".", ",") if t["pnl_pct"] is not None else "—"
                text += f"\nResultado de la operación: {money(t['pnl'])} ({pct} %)"
            if not led["qty"]:
                self._update_bot(b.id, stop_level=None, target_level=None)
        self.event("fill", text, b.id, notify=True)
        return 1

    # ------------------------------------------------------------------ the daily job
    def tick(self, now: pd.Timestamp | None = None) -> str:
        with self._lock:
            return self._tick(now or pd.Timestamp.now(tz="UTC"))

    def _tick(self, now: pd.Timestamp) -> str:
        bots = self.active_bots()
        if not bots:
            self.state = "sin bots en paper trading"
            return self.state
        broker = self.broker()
        if broker is None:
            self.state = "Alpaca no está conectado (Ajustes)"
            return self.state
        try:
            self.sync_orders(broker)
            clock = broker.clock()
            positions = broker.positions()
        except BrokerError as e:
            self.state = f"no se puede conectar con Alpaca: {e}"
            return self.state
        due = due_session(now, self.delay_min)
        todo = [b for b in bots if b.last_signal_day is None or pd.Timestamp(b.last_signal_day, tz="UTC") < due]
        if not todo:
            self.state = f"al día (última señal: cierre del {due.date()})"
            return self.state
        stale = []
        for b in todo:
            try:
                _res, bars = self.lab.result(b)
                if bars.index[-1] < due:
                    stale.append(b.symbol)
            except (KeyError, ValueError):
                stale.append(b.symbol)
        if stale:
            n, last = self._attempts.get(due.date(), (0, None))
            if self.data_busy and self.data_busy():
                self.state = "esperando a que termine una descarga de precios"
                return self.state
            if n < MAX_UPDATE_ATTEMPTS and (last is None or now - last >= pd.Timedelta(minutes=RETRY_MINUTES)):
                if self.refresh and self.refresh(sorted(set(stale))):
                    self._attempts[due.date()] = (n + 1, now)
                    self.event("info", f"Descargando los precios del {due.date()} para las señales de los bots")
                self.state = f"descargando los precios del {due.date()}"
                return self.state
            if n < MAX_UPDATE_ATTEMPTS:
                self.state = f"esperando los precios del {due.date()}"
                return self.state
            self.event("skip", f"⚠️ No hay precios del {due.date()} para {', '.join(sorted(set(stale)))}: "
                               "esos bots no operan hoy.", notify=True)
        entries_ok = not clock.get("is_open")
        for b in todo:
            if b.symbol in stale:
                self._update_bot(b.id, last_signal_day=due.date())
                continue
            try:
                self._process(broker, b, due, positions, entries_ok)
            except BrokerError as e:
                self.event("error", f"Alpaca: {e}", b.id)
                continue
            self._update_bot(b.id, last_signal_day=due.date())
        self.state = f"señales del cierre del {due.date()} procesadas"
        return self.state

    def _process(self, broker, b: m.LabBot, due: pd.Timestamp, positions: dict, entries_ok: bool) -> None:
        st = REGISTRY[b.strategy]
        res, bars = self.lab.result(b)
        sig = res.signals.loc[due] if due in res.signals.index else None
        led = self.ledger(b)
        pos = led["qty"]
        have = (positions.get(b.symbol) or {}).get("qty", 0.0)
        if abs(have - pos) > 1e-6:
            self.event("error", f"⚠️ Descuadre en {b.symbol}: el bot espera {pos:g} acciones y Alpaca tiene {have:g}. "
                                "No se envía nada para este bot hasta que lo revises (¿operaste a mano?).", b.id, notify=True)
            return
        if any(o.status in OPEN_ORDER and o.purpose in ("entry", "exit") for o in self.orders(b.id)):
            self.event("info", f"{b.symbol}: hay una orden pendiente de ejecutarse; se espera a ella.", b.id)
            return
        close = float(bars["close"].iloc[-1])
        d = int(np.sign(pos))
        if sig is None:
            return
        # 1) exits (always sent, even late: getting out matters more than the price)
        flip = d != 0 and int(sig["entry"]) == -d
        if d != 0 and ((d > 0 and sig["exit_long"]) or (d < 0 and sig["exit_short"]) or flip):
            self._cancel_open(broker, b, ("protect", "stop", "target"))
            self._cancel_legs(broker, b)
            o = self._submit(broker, b, "sell" if d > 0 else "buy", abs(pos), "exit", due.date())
            if o is not None:
                self.event("order", f"🔔 Señal de salida en {b.symbol} (cierre del {due.date()}): orden de "
                                    f"{'VENTA' if d > 0 else 'RECOMPRA'} de {abs(pos):g} acciones para la apertura — "
                                    f"bot «{st.name}»", b.id, notify=True)
            if flip:  # the other side opens at the following open, once this exit is filled
                self._update_bot(b.id, pending={"direction": -d, "reason": "giro de la estrategia",
                                                "stop": None, "target": None, "signal_day": str(due.date())})
            return
        # 2) entries: from today's signal, or one waiting (reversal / following the backtest)
        want, why, stop, target = 0, "", None, None
        if d == 0 and b.pending:
            want, why = int(b.pending["direction"]), b.pending.get("reason", "")
            stop, target = b.pending.get("stop"), b.pending.get("target")
        if d == 0 and int(sig["entry"]) != 0:
            want, why = int(sig["entry"]), "señal de entrada"
            stop = sig["stop"] if np.isfinite(sig["stop"]) else (
                close * (1 - want * sig["stop_pct"]) if np.isfinite(sig["stop_pct"]) else None)
            target = sig["target"] if np.isfinite(sig["target"]) else (
                close * (1 + want * sig["target_pct"]) if np.isfinite(sig["target_pct"]) else None)
        if d == 0 and want:
            if not entries_ok:
                self.event("skip", f"⏭️ {b.symbol}: la señal de entrada del {due.date()} no se envía porque la bolsa ya "
                                   "ha abierto (la app no estaba abierta antes de la apertura).", b.id, notify=True)
                self._update_bot(b.id, pending=None)
                return
            self._enter(broker, b, st, want, why, close, stop, target, due)
            return
        # 3) an open position: keep its stop / target resting at Alpaca (trailing stops move only in its favour)
        if d != 0:
            new_stop = b.stop_level
            if np.isfinite(sig["trail"]):
                t = float(sig["trail"])
                new_stop = t if new_stop is None else (max(new_stop, t) if d > 0 else min(new_stop, t))
            self._protect(broker, b, d, abs(pos), new_stop, b.target_level, due)

    def _enter(self, broker, b, st, direction: int, why: str, close: float, stop, target, due) -> None:
        if direction < 0:
            acct, asset = broker.account(), broker.asset(b.symbol)
            if not (acct.get("shorting_enabled") and asset.get("shortable") and asset.get("easy_to_borrow")):
                self.event("skip", f"⏭️ {b.symbol}: Alpaca no permite ponerse corto ahora en esta acción; se omite la entrada.",
                           b.id, notify=True)
                self._update_bot(b.id, pending=None)
                return
        led = self.ledger(b)
        qty = math.floor(led["equity"] * (b.size_pct or 100) / 100 / close)
        if qty < 1:
            self.event("skip", f"⏭️ {b.symbol}: el capital del bot ({money(led['equity'])}) no llega para 1 acción.",
                       b.id, notify=True)
            return
        if stop is not None and direction * (close - stop) <= 0:
            stop = None
        if target is not None and direction * (target - close) <= 0:
            target = None
        o = self._submit(broker, b, "buy" if direction > 0 else "sell", qty, "entry", due.date(), stop, target)
        self._update_bot(b.id, pending=None, stop_level=stop, target_level=target)
        if o is not None:
            extra = (f" · stop {money(stop)}" if stop else "") + (f" · objetivo {money(target)}" if target else "")
            self.event("order", f"🔔 {why.capitalize()} en {b.symbol} (cierre del {due.date()}): orden de "
                                f"{'COMPRA' if direction > 0 else 'VENTA EN CORTO'} de {qty} acciones para la apertura "
                                f"(≈ {money(qty * close)}){extra} — bot «{st.name}»", b.id, notify=True)

    def _cancel_legs(self, broker, b) -> None:
        """Stop / target legs of this bot's bracket entries that are still waiting."""
        for o in self.orders(b.id):
            if o.purpose in ("stop", "target") and o.status in OPEN_ORDER and o.broker_id:
                try:
                    broker.cancel(o.broker_id)
                except BrokerError:
                    pass

    def _protect(self, broker, b, d: int, qty: float, stop, target, due) -> None:
        if stop is None and target is None:
            return
        live = [o for o in self.orders(b.id) if o.purpose in ("protect", "stop", "target") and o.status in OPEN_ORDER]
        same = live and all((o.stop_price is None or stop is None or abs(o.stop_price - stop) < 0.005) for o in live)
        if same:
            return
        for o in live:
            try:
                broker.cancel(o.broker_id)
            except BrokerError:
                pass
        side = "sell" if d > 0 else "buy"
        o = self._submit(broker, b, side, qty, "protect", due.date(), stop, target, tif="gtc")
        if o is None:  # GTC not accepted for this order: one day at a time (re-sent every evening)
            o = self._submit(broker, b, side, qty, "protect", due.date(), stop, target, tif="day")
        self._update_bot(b.id, stop_level=stop, target_level=target)
        if o is not None:
            self.event("order", f"🛡️ {b.symbol}: stop{' y objetivo' if target else ''} colocados en Alpaca "
                                f"(stop {money(stop)}{', objetivo ' + money(target) if target else ''}).", b.id)

    # ------------------------------------------------------------------ background thread
    def start(self, poll_seconds: float = 120.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            self._stop.wait(20)  # let the app finish starting
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - the trader must never take the app down
                    self.state = f"error: {e!r}"[:200]
                    self.logs.append(f"{datetime.now().strftime('%d/%m %H:%M')}  error {e!r}"[:300])
                self._stop.wait(poll_seconds)
        self._thread = threading.Thread(target=loop, daemon=True, name="paper-trader")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
