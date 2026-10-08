"""The library of bots (strategy x stock) and everything shown about each one. Results are recomputed only when the
strategy's code, the bot's settings or the stored prices change."""
from __future__ import annotations

import json
import re
import threading
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import select

from qsts.db import models as m
from qsts.lab import metrics
from qsts.lab.audit import audit as run_audit
from qsts.lab.backtest import REASONS, BacktestConfig, BacktestResult, buy_and_hold, run_backtest
from qsts.lab.strategy import REGISTRY, load_all
from qsts.core.jsonsafe import clean as _clean

CAPITAL = 10_000.0
DEFAULT_CFG = BacktestConfig(initial_capital=CAPITAL)


def bot_id(strategy: str, symbol: str, params: dict | None = None) -> str:
    base = f"{strategy}-{symbol}".lower()
    if params:
        tail = "-".join(f"{k}{v}" for k, v in sorted(params.items()))
        base += "-" + re.sub(r"[^a-z0-9._]+", "", tail.lower())[:24]
    return base[:64]


def _points(s: pd.Series, scale: float = 1.0) -> list[dict]:
    return [{"time": int(t.timestamp()), "value": round(float(v) * scale, 4)} for t, v in s.items() if np.isfinite(v)]


class LabService:
    def __init__(self, sf, frame: Callable[[str], pd.DataFrame], token: Callable[[str], tuple],
                 basket: Callable[[], list[str]] | None = None, benchmark: str = "SPY"):
        self.sf, self.frame, self.token, self.basket, self.benchmark = sf, frame, token, basket, benchmark
        self._cache: dict[str, tuple] = {}
        self._audits: dict[str, tuple] = {}
        self._lock = threading.Lock()
        load_all()

    # ------------------------------------------------------------------ bots
    def sync(self) -> int:
        """Create the default bots of every strategy (a bot the user removed is not created again)."""
        added = 0
        with self.sf() as s, s.begin():
            have = {b.id for b in s.scalars(select(m.LabBot))}
            for key, st in sorted(REGISTRY.items()):
                for sym in st.default_symbols:
                    bid = bot_id(key, sym)
                    if bid not in have:
                        s.add(m.LabBot(id=bid, strategy=key, symbol=sym, params={}))
                        added += 1
        return added

    def bots(self, include_hidden: bool = False) -> list[m.LabBot]:
        with self.sf() as s:
            q = select(m.LabBot).order_by(m.LabBot.created_at)
            if not include_hidden:
                q = q.where(m.LabBot.hidden.is_(False))
            return [b for b in s.scalars(q) if b.strategy in REGISTRY]

    def get_bot(self, bid: str) -> m.LabBot:
        with self.sf() as s:
            b = s.get(m.LabBot, bid)
        if b is None or b.strategy not in REGISTRY:
            raise KeyError(bid)
        return b

    def create_bot(self, strategy: str, symbol: str, params: dict | None = None) -> m.LabBot:
        if strategy not in REGISTRY:
            raise KeyError(strategy)
        symbol = symbol.strip().upper().replace(".", "-")
        if not re.fullmatch(r"[A-Z0-9\-]{1,12}", symbol):
            raise ValueError("símbolo no válido")
        params = {k: v for k, v in (params or {}).items() if REGISTRY[strategy].params.get(k) != v}
        REGISTRY[strategy].resolve(params)  # unknown parameters are refused
        bid = bot_id(strategy, symbol, params)
        with self.sf() as s, s.begin():
            b = s.get(m.LabBot, bid)
            if b is None:
                b = m.LabBot(id=bid, strategy=strategy, symbol=symbol, params=params)
                s.add(b)
            b.hidden = False
        return self.get_bot(bid)

    def update_bot(self, bid: str, **fields) -> m.LabBot:
        allowed = {"favorite", "hidden", "size_pct"}
        with self.sf() as s, s.begin():
            b = s.get(m.LabBot, bid)
            if b is None:
                raise KeyError(bid)
            for k, v in fields.items():
                if k not in allowed or v is None:
                    continue
                if k == "size_pct" and not 0 < float(v) <= 100:
                    raise ValueError("el tamaño debe estar entre 1 y 100% (sin apalancamiento)")
                if k == "hidden" and v and b.paper_status == "active":
                    raise ValueError("detén antes su paper trading")
                setattr(b, k, v)
        return self.get_bot(bid)

    def n_tested(self) -> int:
        """Bots backtested in the app (each one is one more try: more tries, more chances of a lucky result)."""
        with self.sf() as s:
            return len(list(s.scalars(select(m.LabBot.id))))

    # ------------------------------------------------------------------ results
    def config(self, b: m.LabBot) -> BacktestConfig:
        return BacktestConfig(initial_capital=CAPITAL, size_pct=b.size_pct or 100.0)

    def _key(self, b: m.LabBot) -> str:
        st = REGISTRY[b.strategy]
        return json.dumps([b.id, st.version(), b.params or {}, b.size_pct, list(map(str, self.token(b.symbol)))],
                          sort_keys=True, default=str)

    def result(self, b: m.LabBot) -> tuple[BacktestResult, pd.DataFrame]:
        key = self._key(b)
        with self._lock:
            hit = self._cache.get(b.id)
        if hit is not None and hit[0] == key:
            return hit[1], hit[2]
        bars = self.frame(b.symbol)
        res = run_backtest(REGISTRY[b.strategy], bars, b.params or {}, self.config(b))
        with self._lock:
            self._cache[b.id] = (key, res, bars)
        return res, bars

    def row(self, b: m.LabBot) -> dict:
        st = REGISTRY[b.strategy]
        base = {"id": b.id, "strategy": b.strategy, "name": st.name, "symbol": b.symbol, "timeframe": "1 día",
                "favorite": bool(b.favorite), "paper_status": b.paper_status, "allow_short": st.allow_short,
                "custom": bool(b.params)}
        try:
            res, _bars = self.result(b)
        except (KeyError, ValueError) as e:
            return {**base, "error": f"sin datos de {b.symbol}: descárgalos en Datos" if isinstance(e, KeyError)
                    else str(e)[:200]}
        sm = metrics.summary(res.equity, res.trades, CAPITAL, res.position)
        return _clean({**base, **sm, "spark": metrics.sparkline(res.equity),
                       "in_position": res.open_trade is not None})

    def library(self) -> dict:
        rows = [self.row(b) for b in self.bots()]
        return _clean({"rows": rows, "n_bots": len(rows), "n_strategies": len(REGISTRY), "n_tested": self.n_tested(),
                       "strategies": [REGISTRY[k].info() for k in sorted(REGISTRY)]})

    def detail(self, bid: str) -> dict:
        b = self.get_bot(bid)
        st = REGISTRY[b.strategy]
        res, bars = self.result(b)
        eq, tr = res.equity, res.trades
        hold = buy_and_hold(bars, eq.index[0], eq.index[-1], CAPITAL) if len(eq) else eq
        last_sig = res.signals.iloc[-1] if len(res.signals) else None
        nxt = None
        if last_sig is not None:
            pos = np.sign(res.position.iloc[-1]) if len(res.position) else 0
            if pos > 0 and last_sig["exit_long"] or pos < 0 and last_sig["exit_short"]:
                nxt = "cerrar la posición en la próxima apertura"
            elif last_sig["entry"] and last_sig["entry"] != pos:
                nxt = ("comprar" if last_sig["entry"] > 0 else "vender en corto") + " en la próxima apertura"
        trades = []
        for t in tr.iloc[::-1].head(500).itertuples(index=False):
            trades.append({"side": t.side, "entry": str(t.entry_time.date()), "exit": str(t.exit_time.date()),
                           "entry_price": t.entry_price, "exit_price": t.exit_price, "qty": t.qty, "pnl": t.pnl,
                           "pnl_pct": t.pnl_pct, "bars": int(t.bars), "reason": REASONS.get(t.reason, t.reason)})
        ot = res.open_trade
        return _clean({
            "bot": {"id": b.id, "strategy": st.info(), "symbol": b.symbol, "params": st.resolve(b.params or {}),
                    "custom_params": b.params or {}, "size_pct": b.size_pct, "favorite": bool(b.favorite),
                    "paper_status": b.paper_status, "allocation_pct": b.allocation_pct, "capital": b.capital,
                    "activated_at": b.activated_at.isoformat() if b.activated_at else None},
            "capital": CAPITAL, "config": {"size_pct": res.config.size_pct, "slippage_bps": res.config.slippage_bps,
                                           "commission_pct": res.config.commission_pct},
            "summary": metrics.summary(eq, tr, CAPITAL, res.position),
            "key_metrics": metrics.key_metrics(eq, tr),
            "by_side": metrics.report_by_side(tr, CAPITAL),
            "monthly": metrics.monthly_returns(eq), "weekday": metrics.weekday_exposure(res.position),
            "yearly": metrics.yearly(eq, hold),
            "equity": _points(eq / CAPITAL - 1, 100), "hold": _points(hold / CAPITAL - 1, 100),
            "drawdown": _points(metrics.drawdown(eq), 100),
            "pnl_range": {"strategy": metrics.pnl_range(eq, CAPITAL), "hold": metrics.pnl_range(hold, CAPITAL)},
            "trades": trades, "n_trades": int(len(tr)),
            "open_trade": None if ot is None else {
                "side": ot["side"], "entry": str(ot["entry_time"].date()), "entry_price": ot["entry_price"],
                "price": ot["exit_price"], "pnl_pct": ot["pnl_pct"], "stop": ot.get("stop"), "target": ot.get("target")},
            "next_action": nxt, "last_bar": str(bars.index[-1].date()),
            "monte_carlo": metrics.monte_carlo(tr)})

    def audit(self, bid: str) -> dict:
        b = self.get_bot(bid)
        key = self._key(b)
        with self._lock:
            hit = self._audits.get(bid)
        if hit is not None and hit[0] == key:
            return hit[1]
        _res, bars = self.result(b)
        basket = {}
        for sym in (self.basket() if self.basket else []):
            if sym == b.symbol:
                continue
            try:
                basket[sym] = self.frame(sym)
            except (KeyError, ValueError):
                continue
        out = _clean(run_audit(REGISTRY[b.strategy], bars, b.params or {}, self.config(b), basket, self.n_tested()))
        with self._lock:
            self._audits[bid] = (key, out)
        return out
