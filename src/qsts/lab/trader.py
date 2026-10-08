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
from qsts.lab.service import UNIVERSE_NAME, NotReady, is_universe
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


def opg_closed(now: pd.Timestamp) -> bool:
    """Alpaca rejects opening-auction (OPG) orders sent between 9:28 and 19:00 New York time (alpaca-py
    TimeInForce docs); outside that window they are queued for the next opening auction."""
    et = now.tz_convert("America/New_York")
    t = et.hour * 60 + et.minute
    return 9 * 60 + 28 <= t < 19 * 60


def next_session(day: pd.Timestamp) -> pd.Timestamp:
    sched = nyse_schedule(day + pd.Timedelta(days=1), day + pd.Timedelta(days=12))
    return sched.index[0]


MOC_FROM, MOC_LAST = 15 * 60 + 40, 15 * 60 + 50  # New York time: close day trades with a market-on-close order


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
                 delay_min: int = 45, blocked: Callable[[], str | None] | None = None,
                 enabled: Callable[[], bool] | None = None):
        self.sf, self.lab, self.broker, self.telegram = sf, lab, broker, telegram
        self.refresh, self.data_busy, self.delay_min, self.blocked = refresh, data_busy, delay_min, blocked
        self.enabled = enabled or (lambda: True)
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

    # ------------------------------------------------------------------ the bot's state per stock
    @staticmethod
    def book(b: m.LabBot) -> dict:
        """{symbol: {"stop", "target", "pending"}}: protective levels of each open paper position and entries waiting
        for the next open (older versions kept them in single columns: read once from there)."""
        bk = {k: dict(v) for k, v in (b.book or {}).items()}
        if not bk and not is_universe(b) and (b.pending or b.stop_level or b.target_level):
            bk[b.symbol] = {"stop": b.stop_level, "target": b.target_level, "pending": b.pending}
        return bk

    def _save_book(self, bid: str, bk: dict) -> None:
        bk = {k: v for k, v in bk.items() if v.get("pending") or v.get("deferred") or v.get("stop") is not None
              or v.get("target") is not None}
        self._update_bot(bid, book=bk, pending=None, stop_level=None, target_level=None)

    # ------------------------------------------------------------------ ledger of one bot (from its fills)
    def ledger(self, b: m.LabBot, symbol: str | None = None) -> dict:
        """Position, average price, realised P&L and closed trades in one stock, replayed from the bot's fills."""
        symbol = symbol or b.symbol
        qty = avg = realized = 0.0
        trades, opened = [], None
        fills = sorted((o for o in self.orders(b.id) if o.symbol == symbol and (o.filled_qty or 0) > 0 and o.filled_price),
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
                trades.append({"symbol": symbol, "side": "largo" if qty > 0 else "corto",
                               "entry": (opened.filled_at or opened.submitted_at).date().isoformat() if opened else None,
                               "exit": (o.filled_at or o.submitted_at).date().isoformat(), "entry_price": avg,
                               "exit_price": o.filled_price, "qty": closed, "pnl": pnl,
                               "pnl_pct": pnl / (closed * avg) if avg else None, "reason": PURPOSE.get(o.purpose, o.purpose)})
                qty += q
                if abs(qty) < 1e-9:
                    qty, avg, opened = 0.0, 0.0, None
        return {"symbol": symbol, "qty": qty, "avg": avg, "realized": realized, "trades": trades,
                "equity": (b.capital or 0.0) + realized}

    def ledgers(self, b: m.LabBot) -> dict[str, dict]:
        """One ledger per stock the bot has traded."""
        syms = sorted({o.symbol for o in self.orders(b.id)} | ({b.symbol} if not is_universe(b) else set()))
        return {s: self.ledger(b, s) for s in syms}

    def summary(self, b: m.LabBot) -> dict:
        """The bot's whole paper book: open positions, realised P&L and every closed trade (all stocks)."""
        leds = self.ledgers(b)
        trades = sorted((t for led in leds.values() for t in led["trades"]), key=lambda t: t["exit"] or "")
        return {"positions": {s: led for s, led in leds.items() if led["qty"]},
                "realized": sum(led["realized"] for led in leds.values()), "trades": trades}

    def live_curve(self, b: m.LabBot, bars: pd.DataFrame, closes: Callable[[str], pd.Series] | None = None) -> pd.Series:
        """The bot's paper equity at each close since it was activated (fills replayed day by day). `bars` gives
        the calendar (and the prices of a stock bot); `closes(symbol)` the prices of the other stocks."""
        if not b.activated_at:
            return pd.Series(dtype=float)
        start = pd.Timestamp(b.activated_at).tz_localize("UTC").normalize()
        cal = bars["close"][bars.index >= start]
        if not len(cal):
            return pd.Series(dtype=float)
        fills = sorted((o for o in self.orders(b.id) if (o.filled_qty or 0) > 0 and o.filled_price),
                       key=lambda o: o.filled_at or o.submitted_at)
        px: dict[str, pd.Series] = {}
        for sym in {o.symbol for o in fills}:
            if not is_universe(b) and sym == b.symbol:
                px[sym] = cal
                continue
            try:
                px[sym] = (closes(sym) if closes else pd.Series(dtype=float)).reindex(cal.index, method="ffill")
            except (KeyError, ValueError):
                px[sym] = pd.Series(np.nan, index=cal.index)
        cash, qty, k, out = b.capital or 0.0, {}, 0, []
        last: dict[str, float] = {}
        for day in cal.index:
            end = day + pd.Timedelta(days=1)
            while k < len(fills) and pd.Timestamp(fills[k].filled_at or fills[k].submitted_at).tz_localize("UTC") < end:
                o = fills[k]
                q = o.filled_qty if o.side == "buy" else -o.filled_qty
                cash -= q * o.filled_price
                qty[o.symbol] = qty.get(o.symbol, 0.0) + q
                last.setdefault(o.symbol, o.filled_price)
                k += 1
            value = 0.0
            for sym, q in qty.items():
                p = px[sym].get(day, np.nan)
                if np.isfinite(p):
                    last[sym] = float(p)
                value += q * last.get(sym, 0.0)
            out.append(cash + value)
        return pd.Series(out, index=cal.index)

    # ------------------------------------------------------------------ activation
    def _claims(self, exclude: str | None = None) -> dict[str, str]:
        """Stocks already used by an active bot (a stock bot always claims its stock; a portfolio bot the stocks
        it holds or is about to buy): two bots on the same stock would share one Alpaca position."""
        out = {}
        for x in self.active_bots():
            if x.id == exclude:
                continue
            if not is_universe(x):
                out[x.symbol] = x.id
                continue
            for s, led in self.ledgers(x).items():
                if led["qty"]:
                    out[s] = x.id
            for s, v in self.book(x).items():
                if v.get("pending"):
                    out[s] = x.id
            for o in self.orders(x.id):
                if o.purpose == "entry" and o.status in OPEN_ORDER:
                    out[o.symbol] = x.id
        return out

    def activate(self, bid: str, allocation_pct: float, follow_open: bool = True, now: pd.Timestamp | None = None) -> dict:
        with self._lock:
            b = self.lab.get_bot(bid)
            if b.paper_status == "active":
                raise ValueError("este bot ya está en paper trading")
            if not 1 <= allocation_pct <= 100:
                raise ValueError("la parte de la cuenta debe estar entre 1 y 100%")
            if not self.enabled():
                raise ValueError("en este ordenador el paper trading está desactivado (lo hace el servidor): "
                                 "actívalo desde la app del servidor")
            broker = self.broker()
            if broker is None:
                raise ValueError("conecta primero tu cuenta paper de Alpaca (Ajustes)")
            if not is_universe(b):
                owner = self._claims().get(b.symbol)
                if owner:
                    raise ValueError(f"ya hay un bot activo con {b.symbol} ({owner}): dos bots en la misma acción "
                                     "se mezclarían en la misma posición de Alpaca")
            used = sum(x.allocation_pct or 0 for x in self.active_bots())
            if used + allocation_pct > 100:
                raise ValueError(f"ya tienes asignado el {used:g}% de la cuenta: no se puede pasar del 100% (sin apalancamiento)")
            follow = []
            if follow_open:  # the backtest is inside trades right now: open the same positions at the next open
                try:
                    follow = self.lab.open_trades(b)
                except NotReady:
                    raise ValueError("la cartera todavía se está calculando: espera a que termine y vuelve a activarla")
            acct = broker.account()
            if acct.get("trading_blocked") or acct.get("account_blocked"):
                raise ValueError("Alpaca indica que la cuenta tiene el trading bloqueado")
            capital = float(acct["equity"]) * allocation_pct / 100
            taken = self._claims(exclude=bid)
            bk = {}
            for t in follow:
                if t["symbol"] in taken:
                    continue
                bk[t["symbol"]] = {"pending": {"direction": int(t["direction"]),
                                               "reason": "seguir la operación abierta del backtest",
                                               "stop": t.get("stop"), "target": t.get("target"),
                                               "created": _now().isoformat()}}
            at = (now or pd.Timestamp.now(tz="UTC")).tz_convert("UTC").tz_localize(None).to_pydatetime()
            self._update_bot(bid, paper_status="active", allocation_pct=allocation_pct, capital=capital,
                             activated_at=at, stopped_at=None, last_signal_day=None, stop_level=None,
                             target_level=None, pending=None, book=bk)
            st = REGISTRY[b.strategy]
            what = f"cartera {UNIVERSE_NAME} (máx. {b.max_positions} posiciones)" if is_universe(b) else b.symbol
            follow_txt = ""
            if bk:
                follow_txt = (" — entrará en la próxima apertura como el backtest" if not is_universe(b) else
                              f" — en la próxima apertura comprará lo que tiene abierto el backtest: {', '.join(sorted(bk))}")
            self.event("info", f"▶️ Bot activado en paper: «{st.name}» con {what}, {allocation_pct:g}% de la cuenta "
                               f"({money(capital)})" + follow_txt, bid, notify=True)
            return {"capital": capital, "follow": bool(bk), "symbols": sorted(bk)}

    def deactivate(self, bid: str, close: bool = True) -> dict:
        with self._lock:
            b = self.lab.get_bot(bid)
            if b.paper_status != "active":
                raise ValueError("este bot no está en paper trading")
            if not self.enabled():  # the server owns these orders: touching them from here would undo its work
                raise ValueError("en este ordenador el paper trading está desactivado (lo hace el servidor): "
                                 "detén el bot desde la app del servidor")
            broker = self.broker()
            closed = []
            if broker is not None:
                self._cancel_open(broker, b)
                for sym, led in self.ledgers(b).items():
                    if close and led["qty"]:
                        self._submit(broker, b, sym, "sell" if led["qty"] > 0 else "buy", abs(led["qty"]), "exit", None)
                        closed.append(sym)
            self._update_bot(bid, paper_status="stopped", stopped_at=_now(), pending=None, book={})
            self.event("info", f"⏹️ Bot detenido: {bid}" + (f" (se cierra{'n' if len(closed) > 1 else ''} "
                                                              f"{', '.join(closed)} en la próxima apertura)" if closed else ""),
                       bid, notify=True)
            return {"closing": bool(closed), "symbols": closed}

    # ------------------------------------------------------------------ orders
    def _client_id(self, b: m.LabBot, purpose: str, symbol: str = "") -> str:
        return f"qsts-{hash_obj([b.id, symbol, purpose, _now().isoformat()], 10)}-{purpose[:3]}"

    def _submit(self, broker, b: m.LabBot, symbol: str, side: str, qty: float, purpose: str, signal_day,
                stop: float | None = None, target: float | None = None, order_type: str = "market",
                tif: str = "day", level: float | None = None, simple: bool = False) -> m.LabOrder | None:
        cid = self._client_id(b, purpose, symbol)
        req = {"symbol": symbol, "qty": int(qty), "side": side, "type": order_type, "time_in_force": tif,
               "client_order_id": cid}
        kind = order_type if tif not in ("opg", "cls") else f"{order_type}-{tif}"
        if order_type == "stop" and purpose in ("entry", "exit"):
            req["stop_price"] = level
        elif order_type == "limit" and purpose in ("entry", "exit"):
            req["limit_price"] = level
        if simple or order_type != "market":
            pass  # stop / target are placed once the entry is filled (protective orders)
        elif purpose == "entry" and (stop or target):
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
        if purpose in ("entry", "exit") and level is not None:
            stop, target = (level, None) if order_type == "stop" else (None, level)
        row = m.LabOrder(id=cid, bot_id=b.id, symbol=symbol, side=side, qty=int(qty), purpose=purpose,
                         order_type=kind, stop_price=stop, limit_price=target, signal_day=signal_day)
        try:
            o = broker.submit(req)
        except BrokerError as e:
            row.status, row.error = "rejected", str(e)[:500]
            with self.sf() as s, s.begin():
                s.add(row)
            self.event("error", f"⚠️ Alpaca rechazó la orden de {PURPOSE.get(purpose, purpose)} de {symbol} ({b.id}): {e}",
                       b.id, notify=True)
            return None
        row.broker_id, row.status, row.raw = o.get("id"), o.get("status") or "submitted", o
        with self.sf() as s, s.begin():
            s.add(row)
        return row

    def _cancel_open(self, broker, b: m.LabBot, purposes: tuple = ("entry", "exit", "protect", "stop", "target"),
                     symbol: str | None = None) -> int:
        n = 0
        for o in self.orders(b.id):
            if symbol is not None and o.symbol != symbol:
                continue
            if o.purpose in purposes and o.status in OPEN_ORDER and o.broker_id:
                try:
                    broker.cancel(o.broker_id)
                    n += 1
                except BrokerError as e:
                    self.event("error", f"No se pudo cancelar una orden de {o.symbol}: {e}", b.id)
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
                if row.purpose == "entry" and row.order_type in ("stop", "limit-opg"):  # conditional entry: normal
                    self.event("info", f"{row.symbol}: la orden de entrada condicionada no se ejecutó (el precio no "
                                       "llegó a su nivel). Es lo normal en esta estrategia.", row.bot_id)
                    return 0
                self.event("skip", f"⚠️ La orden de {PURPOSE[row.purpose]} de {row.symbol} quedó «{status}» sin ejecutarse "
                                   f"({row.bot_id}).", row.bot_id, notify=True)
            return 0
        b = self.lab.get_bot(row.bot_id)
        verb = {"buy": "compradas", "sell": "vendidas"}[row.side]
        icon = {"stop": "🛑 Stop ejecutado", "target": "🎯 Objetivo alcanzado"}.get(row.purpose, "✅ Ejecutada")
        text = f"{icon}: {verb} {filled_qty:g} {row.symbol} a {money(price)} — bot «{REGISTRY[b.strategy].name}»"
        if is_universe(b):
            text += f" ({UNIVERSE_NAME})"
        led = self.ledger(b, row.symbol)
        if row.purpose in ("exit", "stop", "target"):
            if led["trades"]:
                t = led["trades"][-1]
                pct = f"{t['pnl_pct'] * 100:+.2f}".replace(".", ",") if t["pnl_pct"] is not None else "—"
                text += f"\nResultado de la operación: {money(t['pnl'])} ({pct} %)"
        if not led["qty"]:  # flat in this stock: its protective levels are gone
            bk = self.book(b)
            if row.symbol in bk:
                bk[row.symbol].update(stop=None, target=None)
                self._save_book(b.id, bk)
        self.event("fill", text, b.id, notify=True)
        return 1

    # ------------------------------------------------------------------ the daily job
    def tick(self, now: pd.Timestamp | None = None) -> str:
        with self._lock:
            return self._tick(now or pd.Timestamp.now(tz="UTC"))

    def _tick(self, now: pd.Timestamp) -> str:
        if not self.enabled():
            self.state = "desactivado en este ordenador (el paper trading lo hace el servidor)"
            return self.state
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
            self._send_deferred(broker, now)
            self._intraday(broker, now, clock)
        except BrokerError as e:
            self.state = f"no se puede conectar con Alpaca: {e}"
            return self.state
        due = due_session(now, self.delay_min)
        todo = [b for b in bots if b.last_signal_day is None or pd.Timestamp(b.last_signal_day, tz="UTC") < due]
        if not todo:
            self.state = f"al día (última señal: cierre del {due.date()})"
            return self.state
        stale = sorted({s for b in todo for s in self.lab.stale_symbols(b, due)})
        if stale:
            n, last = self._attempts.get(due.date(), (0, None))
            if self.data_busy and self.data_busy():
                self.state = "esperando a que termine una descarga de precios"
                return self.state
            if n < MAX_UPDATE_ATTEMPTS and (last is None or now - last >= pd.Timedelta(minutes=RETRY_MINUTES)):
                if self.refresh and self.refresh(stale):
                    self._attempts[due.date()] = (n + 1, now)
                    self.event("info", f"Descargando los precios del {due.date()} para las señales de los bots "
                                       f"({len(stale)} valores)")
                self.state = f"descargando los precios del {due.date()}"
                return self.state
            if n < MAX_UPDATE_ATTEMPTS:
                self.state = f"esperando los precios del {due.date()}"
                return self.state
            if not self._attempts.get(("skipped", due.date())):
                self._attempts[("skipped", due.date())] = True
                shown = ", ".join(stale[:8]) + (f" y {len(stale) - 8} más" if len(stale) > 8 else "")
                self.event("skip", f"⚠️ No hay precios del {due.date()} para {shown}: esos valores no operan hoy.",
                           notify=True)
        # what each stock should hold according to the active bots: anything else at Alpaca is a mismatch
        expected: dict[str, float] = {}
        for b in bots:
            for s, led in self.ledgers(b).items():
                expected[s] = expected.get(s, 0.0) + led["qty"]
        mismatch = {s for s in set(expected) | set(positions)
                    if abs((positions.get(s) or {}).get("qty", 0.0) - expected.get(s, 0.0)) > 1e-6}
        entries_ok = not clock.get("is_open")
        waiting = []
        for b in todo:
            view = self.lab.signal_view(b, due)
            if view is None:  # a portfolio bot being recomputed with the new prices: next round
                waiting.append(b.id)
                continue
            if not view["rows"]:
                self._update_bot(b.id, last_signal_day=due.date())
                continue
            try:
                self._process(broker, b, due, view, positions, mismatch, entries_ok, now)
            except BrokerError as e:
                self.event("error", f"Alpaca: {e}", b.id)
                continue
            self._update_bot(b.id, last_signal_day=due.date())
        if waiting:
            self.state = f"calculando las señales de la cartera del {due.date()} ({len(waiting)} bot(s))"
            return self.state
        self.state = f"señales del cierre del {due.date()} procesadas"
        return self.state

    def _process(self, broker, b: m.LabBot, due: pd.Timestamp, view: dict, positions: dict, mismatch: set,
                 entries_ok: bool, now: pd.Timestamp | None = None) -> None:
        now = now if now is not None else pd.Timestamp.now(tz="UTC")
        st = REGISTRY[b.strategy]
        rows, uni = view["rows"], is_universe(b)
        label = f"«{st.name}»" + (f" ({UNIVERSE_NAME})" if uni else "")
        bk = self.book(b)
        leds = self.ledgers(b)
        held = {s: led for s, led in leds.items() if led["qty"]}
        orders = self.orders(b.id)
        busy = {o.symbol for o in orders if o.status in OPEN_ORDER and o.purpose in ("entry", "exit")}
        exiting: set[str] = set()
        # 1) open positions: exits (always sent, even late: getting out matters more than the price) or protection
        for sym, led in held.items():
            pos = led["qty"]
            d = int(np.sign(pos))
            if sym in mismatch:
                have = (positions.get(sym) or {}).get("qty", 0.0)
                self.event("error", f"⚠️ Descuadre en {sym}: el bot espera {pos:g} acciones y Alpaca tiene {have:g}. "
                                    "No se envía nada para esa acción hasta que lo revises (¿operaste a mano?).",
                           b.id, notify=True)
                continue
            if sym in busy:
                self.event("info", f"{sym}: hay una orden pendiente de ejecutarse; se espera a ella.", b.id)
                continue
            if st.day_trade:  # intraday: closed at the close by _intraday (or at the next open if that was missed)
                continue
            sig = rows.get(sym)
            if sig is None:
                self.event("skip", f"{sym}: sin precio del {due.date()}; la posición sigue igual hoy.", b.id)
                continue
            flip = sig["entry"] == -d
            if (d > 0 and sig["exit_long"]) or (d < 0 and sig["exit_short"]) or flip:
                self._cancel_open(broker, b, ("protect", "stop", "target"), symbol=sym)
                o = self._submit(broker, b, sym, "sell" if d > 0 else "buy", abs(pos), "exit", due.date())
                exiting.add(sym)
                if o is not None:
                    self.event("order", f"🔔 Señal de salida en {sym} (cierre del {due.date()}): orden de "
                                        f"{'VENTA' if d > 0 else 'RECOMPRA'} de {abs(pos):g} acciones para la apertura — "
                                        f"bot {label}", b.id, notify=True)
                if flip:  # the other side opens at the following open, once this exit is filled
                    bk.setdefault(sym, {})["pending"] = {"direction": -d, "reason": "giro de la estrategia",
                                                         "stop": None, "target": None, "signal_day": str(due.date())}
                continue
            lv = bk.setdefault(sym, {})
            new_stop = lv.get("stop")
            if np.isfinite(sig["trail"]):  # trailing stops move only in the position's favour
                t = float(sig["trail"])
                new_stop = t if new_stop is None else (max(new_stop, t) if d > 0 else min(new_stop, t))
            self._protect(broker, b, sym, d, abs(pos), new_stop, lv.get("target"), due, bk)
        # 2) entries: first those waiting (reversal / following the backtest), then today's best-ranked signals
        occupied = len(held) - len(exiting)
        free = view["max_positions"] - occupied - sum(1 for s in busy if s not in held)
        wanted: list[tuple] = []
        for sym, v in sorted(bk.items()):
            p = v.get("pending")
            if not p or sym in held:
                continue
            wanted.append((sym, int(p["direction"]), p.get("reason", ""), p.get("stop"), p.get("target"), "market", None))
        taken = self._claims(exclude=b.id)
        for sym in view["ranked"]:
            if sym in held or sym in busy or any(w[0] == sym for w in wanted):
                continue
            if sym in taken:
                continue
            sig = rows[sym]
            if sym in mismatch:
                if not uni:
                    have = (positions.get(sym) or {}).get("qty", 0.0)
                    self.event("error", f"⚠️ Descuadre en {sym}: el bot espera 0 acciones y Alpaca tiene {have:g}. "
                                        "No se envía nada para esa acción hasta que lo revises (¿operaste a mano?).",
                               b.id, notify=True)
                continue  # a stock you hold by hand is never bought by a portfolio bot
            want = int(sig["entry"])
            kind, level = "market", None
            if np.isfinite(sig.get("entry_stop", np.nan)):
                kind, level = "stop", float(sig["entry_stop"])
            elif np.isfinite(sig.get("entry_limit", np.nan)):
                kind, level = "limit_open", float(sig["entry_limit"])
            ref = level if kind == "stop" else sig["close"]  # percentages from the expected fill
            stop = sig["stop"] if np.isfinite(sig["stop"]) else (
                ref * (1 - want * sig["stop_pct"]) if np.isfinite(sig["stop_pct"]) else None)
            target = sig["target"] if np.isfinite(sig["target"]) else (
                ref * (1 + want * sig["target_pct"]) if np.isfinite(sig["target_pct"]) else None)
            wanted.append((sym, want, "señal de entrada", stop, target, kind, level))
        wanted = wanted[:max(free, 0)]
        for sym, v in bk.items():  # pending entries are used now (or dropped if there is no place for them)
            if sym not in held:
                v["pending"] = None
        if wanted and not entries_ok:
            names = ", ".join(w[0] for w in wanted)
            self.event("skip", f"⏭️ {names}: la señal de entrada del {due.date()} no se envía porque la bolsa ya "
                               "ha abierto (la app no estaba abierta antes de la apertura).", b.id, notify=True)
            wanted = []
        if wanted:
            equity = (b.capital or 0.0) + sum(led["realized"] for led in leds.values())
            gross = 0.0
            for sym, led in held.items():
                c = rows.get(sym, {}).get("close") or led["avg"]
                equity += led["qty"] * (c - led["avg"])
                if sym not in exiting:
                    gross += abs(led["qty"]) * c
            for sym, direction, why, stop, target, kind, level in wanted:
                close = rows[sym]["close"] if sym in rows else None
                if close is None:
                    continue
                amount = min(equity * view["size"], equity - gross)
                sent = self._enter(broker, b, sym, direction, why, close, stop, target, due, amount, label, bk,
                                   kind=kind, level=level, now=now, day_trade=st.day_trade)
                if sent:
                    gross += sent
        self._save_book(b.id, bk)

    def _enter(self, broker, b, sym: str, direction: int, why: str, close: float, stop, target, due, amount: float,
               label: str, bk: dict, kind: str = "market", level: float | None = None,
               now: pd.Timestamp | None = None, day_trade: bool = False) -> float:
        if direction < 0:
            acct, asset = broker.account(), broker.asset(sym)
            if not (acct.get("shorting_enabled") and asset.get("shortable") and asset.get("easy_to_borrow")):
                self.event("skip", f"⏭️ {sym}: Alpaca no permite ponerse corto ahora en esta acción; se omite la entrada.",
                           b.id, notify=True)
                return 0.0
        ref = level if kind == "stop" and level else close  # a stop entry fills at (or above) its level
        qty = math.floor(max(amount, 0.0) / max(ref, close if kind == "stop" else 0.0))
        if qty < 1:
            self.event("skip", f"⏭️ {sym}: el dinero disponible del bot ({money(amount)}) no llega para 1 acción.",
                       b.id, notify=True)
            return 0.0
        if stop is not None and direction * (ref - stop) <= 0:
            stop = None
        if target is not None and direction * (target - ref) <= 0:
            target = None
        side = "buy" if direction > 0 else "sell"
        how = {"market": "para la apertura", "stop": f"con orden STOP a {money(level)} (solo si el precio llega)",
               "limit_open": f"en la apertura solo si abre a {money(level)} o mejor"}[kind]
        extra = (f" · stop {money(stop)}" if stop else "") + (f" · objetivo {money(target)}" if target else "") +                 (" · se cierra al final del día" if day_trade else "")
        text = (f"🔔 {why.capitalize()} en {sym} (cierre del {due.date()}): orden de "
                f"{'COMPRA' if direction > 0 else 'VENTA EN CORTO'} de {qty} acciones {how} "
                f"(≈ {money(qty * ref)}){extra} — bot {label}")
        bk.setdefault(sym, {}).update(stop=stop, target=target, pending=None)
        if kind == "limit_open":
            order = {"side": side, "qty": qty, "level": level, "signal_day": str(due.date()),
                     "session": str(next_session(due).date()), "text": text}
            if opg_closed(now if now is not None else pd.Timestamp.now(tz="UTC")):
                bk[sym]["deferred"] = order  # Alpaca only accepts it after 19:00 New York time (or before 9:28)
                self.event("info", text + " — se enviará a Alpaca a partir de las 19:00 de Nueva York (01:00 en "
                                          "España) o antes de la apertura, si la app está abierta.", b.id, notify=True)
                return qty * ref
            o = self._submit(broker, b, sym, side, qty, "entry", due.date(), stop, target, order_type="limit",
                             tif="opg", level=level, simple=True)
        elif kind == "stop":
            o = self._submit(broker, b, sym, side, qty, "entry", due.date(), stop, target, order_type="stop",
                             level=level, simple=True)
        else:
            o = self._submit(broker, b, sym, side, qty, "entry", due.date(), stop, target, simple=day_trade)
        if o is None:
            return 0.0
        self.event("order", text, b.id, notify=True)
        return qty * ref

    def _send_deferred(self, broker, now: pd.Timestamp) -> None:
        """Limit-on-open entries that had to wait for Alpaca's OPG window."""
        for b in self.active_bots():
            bk = self.book(b)
            changed = False
            for sym, v in bk.items():
                d = v.get("deferred")
                if not d:
                    continue
                session_open = pd.Timestamp(d["session"]).tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=28)
                if now >= session_open:
                    v["deferred"] = None
                    changed = True
                    self.event("skip", f"⏭️ {sym}: la orden en la apertura del {d['session']} no se pudo enviar a tiempo "
                                       "(la app no estaba abierta en la ventana permitida por Alpaca).", b.id, notify=True)
                    continue
                if opg_closed(now):
                    continue
                o = self._submit(broker, b, sym, d["side"], d["qty"], "entry", pd.Timestamp(d["signal_day"]).date(),
                                 v.get("stop"), v.get("target"), order_type="limit", tif="opg", level=d["level"],
                                 simple=True)
                v["deferred"] = None
                changed = True
                if o is not None:
                    self.event("order", d["text"], b.id, notify=True)
            if changed:
                self._save_book(b.id, bk)

    def _intraday(self, broker, now: pd.Timestamp, clock: dict) -> None:
        """Day trades: protective orders once the entry is filled, and out at the close (market-on-close). A
        position still open after the close (the app was not running) is closed at the next open and reported."""
        et = now.tz_convert("America/New_York")
        mins = et.hour * 60 + et.minute
        for b in self.active_bots():
            st = REGISTRY[b.strategy]
            if not st.day_trade:
                continue
            bk = self.book(b)
            orders = self.orders(b.id)
            for sym, led in self.ledgers(b).items():
                if not led["qty"]:
                    continue
                d = int(np.sign(led["qty"]))
                if any(o.symbol == sym and o.purpose == "exit" and o.status in OPEN_ORDER for o in orders):
                    continue
                side = "sell" if d > 0 else "buy"
                if clock.get("is_open"):
                    if mins >= MOC_FROM:
                        self._cancel_open(broker, b, ("protect", "stop", "target"), symbol=sym)
                        tif = "cls" if mins < MOC_LAST else "day"
                        o = self._submit(broker, b, sym, side, abs(led["qty"]), "exit", et.date(), tif=tif)
                        if o is not None:
                            self.event("order", f"🔔 {sym}: cierre del día (estrategia intradía) — orden de "
                                                f"{'VENTA' if d > 0 else 'RECOMPRA'} de {abs(led['qty']):g} acciones "
                                                f"{'en la subasta de cierre' if tif == 'cls' else 'a mercado'}.", b.id, notify=True)
                    else:
                        lv = bk.get(sym) or {}
                        if lv.get("stop") is not None or lv.get("target") is not None:
                            self._protect(broker, b, sym, d, abs(led["qty"]), lv.get("stop"), lv.get("target"),
                                          et, bk, first_tif="day")
                            self._save_book(b.id, bk)
                else:
                    self._cancel_open(broker, b, ("protect", "stop", "target"), symbol=sym)
                    o = self._submit(broker, b, sym, side, abs(led["qty"]), "exit", et.date())
                    if o is not None:
                        self.event("error", f"⚠️ {sym}: la posición intradía no se cerró al cierre (la app no estaba "
                                            "abierta antes de las 21:50 de España); se cierra en la próxima apertura. "
                                            "Esto la separa del backtest.", b.id, notify=True)

    def _protect(self, broker, b, sym: str, d: int, qty: float, stop, target, due, bk: dict,
                 first_tif: str = "gtc") -> None:
        if stop is None and target is None:
            return
        live = [o for o in self.orders(b.id)
                if o.symbol == sym and o.purpose in ("protect", "stop", "target") and o.status in OPEN_ORDER]
        same = live and all((o.stop_price is None or stop is None or abs(o.stop_price - stop) < 0.005) for o in live)
        if same:
            return
        for o in live:
            try:
                broker.cancel(o.broker_id)
            except BrokerError:
                pass
        side = "sell" if d > 0 else "buy"
        o = self._submit(broker, b, sym, side, qty, "protect", due.date(), stop, target, tif=first_tif)
        if o is None and first_tif != "day":  # GTC not accepted for this order: one day at a time (re-sent every evening)
            o = self._submit(broker, b, sym, side, qty, "protect", due.date(), stop, target, tif="day")
        bk.setdefault(sym, {}).update(stop=stop, target=target)
        if o is not None:
            self.event("order", f"🛡️ {sym}: stop{' y objetivo' if target else ''} colocados en Alpaca "
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
