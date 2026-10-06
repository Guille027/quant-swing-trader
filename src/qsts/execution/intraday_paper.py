"""Day-by-day paper simulation of an intraday rule (Intradía tab).

The rule is frozen when the simulation starts; only sessions AFTER that moment count, so this is a genuine forward
test with data no search has seen. Intraday bars are only stored after each session (Yahoo, downloaded in the
evening), so each day is simulated after the close with the lab's own fills, costs and sizing: it shows what the
rule would have done, it never gives live signals. Each session is recorded once in an append-only journal (later
downloads or data revisions never change a recorded day). Positions are flat every night, so the account (EUR or
USD) carries no overnight currency exposure: each day's result is applied to the account value.
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import select

from qsts.data.bars import nyse_schedule
from qsts.db import models as m
from qsts.execution.paper import PaperError, last_completed_session
from qsts.research.experiments import _clean
from qsts.research.intraday import (BAR_MIN, DATASETS, EXIT_KIND, FAMILY_NAMES, PortfolioConfig, passive_returns,
                                    portfolio_returns, prepare_symbol, rule_from_dict, simulate_symbol)

COMPLETE_SHARE = 0.9  # a session is recorded once this share of the stocks has its bars (or a later one is complete)


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def first_session_after(now) -> pd.Timestamp:
    """The first NYSE session that has not opened yet at `now`."""
    now = _utc(now)
    sched = nyse_schedule(now - pd.Timedelta(days=1), now + pd.Timedelta(days=14))
    return sched.index[sched["market_open"] > now][0]


class IntradayPaper:
    def __init__(self, sf, load: Callable[[str, list[str]], tuple[dict, dict]]):
        """`load(dataset, symbols)` -> (intraday research frames, daily research frames) per symbol."""
        self.sf, self.load = sf, load
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ sessions
    def active(self) -> m.IntradayPaperSession | None:
        with self.sf() as s:
            return s.scalars(select(m.IntradayPaperSession).where(m.IntradayPaperSession.status == "ACTIVE")
                             .order_by(m.IntradayPaperSession.id.desc())).first()

    def latest(self) -> m.IntradayPaperSession | None:
        with self.sf() as s:
            return s.scalars(select(m.IntradayPaperSession).order_by(m.IntradayPaperSession.id.desc())).first()

    def start(self, rule: dict, dataset: str, symbols: list[str], capital: float, currency: str = "EUR",
              candidate_id: str | None = None, pc: PortfolioConfig | None = None, now=None) -> int:
        if self.active() is not None:
            raise PaperError("ya hay una simulación intradía en marcha: detenla antes de empezar otra")
        if dataset not in DATASETS:
            raise PaperError("tipo de velas desconocido")
        if not symbols:
            raise PaperError("la regla no tiene acciones")
        if not capital or capital < 100:
            raise PaperError("el capital debe ser de al menos 100")
        if currency not in ("EUR", "USD"):
            raise PaperError("moneda no admitida (EUR o USD)")
        rule_from_dict(rule)  # must be a valid rule
        start = first_session_after(now if now is not None else pd.Timestamp.now(tz="UTC"))
        with self.sf() as s, s.begin():
            ps = m.IntradayPaperSession(dataset=dataset, rule=rule, candidate_id=candidate_id, symbols=sorted(symbols),
                                        start=start.date(), capital=float(capital), currency=currency,
                                        config=asdict(pc or PortfolioConfig()), status="ACTIVE")
            s.add(ps)
            s.flush()
            return ps.id

    def stop(self, reason: str = "manual") -> None:
        with self.sf() as s, s.begin():
            ps = s.scalars(select(m.IntradayPaperSession).where(m.IntradayPaperSession.status == "ACTIVE")).first()
            if ps is None:
                raise PaperError("no hay ninguna simulación intradía en marcha")
            ps.status, ps.stopped_at, ps.stop_reason = "STOPPED", datetime.now(timezone.utc).replace(tzinfo=None), reason

    def journal(self, session_id: int) -> list[m.IntradayPaperDay]:
        with self.sf() as s:
            return list(s.scalars(select(m.IntradayPaperDay).where(m.IntradayPaperDay.session_id == session_id)
                                  .order_by(m.IntradayPaperDay.day)))

    # ------------------------------------------------------------------ simulation
    def _prepared(self, ps) -> dict:
        bars, daily = self.load(ps.dataset, list(ps.symbols))
        bm = BAR_MIN[ps.dataset]
        return {s: d for s, b in bars.items() if (d := prepare_symbol(b, daily.get(s), bm)) is not None}

    def update(self, days: dict | None = None) -> list[m.IntradayPaperDay]:
        """Record the sessions completed since the last recorded one (append-only). Returns the new days."""
        with self._lock:
            ps = self.active()
            if ps is None:
                return []
            done = self.journal(ps.id)
            if done and pd.Timestamp(done[-1].day, tz="UTC") >= last_completed_session():
                return []  # nothing new can exist yet: do not load any data
            days = self._prepared(ps) if days is None else days
            start = pd.Timestamp(ps.start, tz="UTC")
            counts = Counter(d for sd in days.values() for d in sd.sessions if d >= start)
            complete = [d for d, k in counts.items() if k >= COMPLETE_SHARE * len(ps.symbols)]
            if not complete:
                return []
            last = pd.Timestamp(done[-1].day, tz="UTC") if done else None
            new = pd.DatetimeIndex(sorted(d for d in counts if d <= max(complete) and (last is None or d > last)))
            if len(new) == 0:
                return []
            rule, pc = rule_from_dict(ps.rule), PortfolioConfig(**ps.config)
            bm = BAR_MIN[ps.dataset]
            trades = {s: simulate_symbol(sd, rule, bm, pc.cost_bps) for s, sd in days.items()}
            daily_r, tr = portfolio_returns({s: t[t["session"].isin(new)] for s, t in trades.items()}, new, pc)
            passive = passive_returns(days, new)
            eq = done[-1].equity if done else ps.capital
            rows = []
            for d in new:
                before = eq
                day_tr = tr[tr["session"] == d]
                eq = before * (1 + float(daily_r.loc[d]))
                rows.append(m.IntradayPaperDay(
                    session_id=ps.id, day=d.date(), equity=eq, pnl=eq - before, passive=float(passive.loc[d]),
                    trades=_clean([{"symbol": t.symbol, "side": "largo" if t.direction > 0 else "corto",
                                    "entry_min": int(t.entry_min), "exit": EXIT_KIND[int(t.exit)], "net": float(t.net),
                                    "amount": float(t.weight * before), "pnl": float(t.weight * t.net * before)}
                                   for t in day_tr.itertuples(index=False)])))
            with self.sf() as s, s.begin():
                s.add_all(rows)
            return rows

    def mark_notified(self, day_id: int) -> None:
        with self.sf() as s, s.begin():
            s.get(m.IntradayPaperDay, day_id).notified_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def view(self, now=None, refresh: bool = True) -> dict:
        ps = self.active()
        if ps is not None and refresh:
            self.update()
        ps = ps or self.latest()
        if ps is None:
            return {"active": False}
        days = self.journal(ps.id)
        rule = rule_from_dict(ps.rule)
        eq = days[-1].equity if days else ps.capital
        passive_eq = float(np.prod([1 + (d.passive or 0) for d in days])) if days else 1.0
        n_tr = sum(len(d.trades) for d in days)
        wins = sum(1 for d in days for t in d.trades if t["net"] > 0)
        last_done = last_completed_session(now)
        recorded = pd.Timestamp(days[-1].day, tz="UTC") if days else None
        start = pd.Timestamp(ps.start, tz="UTC")
        if ps.status == "ACTIVE" and last_done >= start:
            sched = nyse_schedule(start, last_done)
            waiting = int((sched.index > recorded).sum()) if recorded is not None else len(sched)
        else:
            waiting = 0
        return _clean({
            "active": ps.status == "ACTIVE", "id": ps.id, "status": ps.status, "dataset": ps.dataset,
            "rule": rule.describe(), "family": FAMILY_NAMES[rule.family], "candidate_id": ps.candidate_id,
            "symbols": len(ps.symbols), "start": str(ps.start), "capital": ps.capital, "currency": ps.currency,
            "equity": eq, "pnl": eq - ps.capital, "return": eq / ps.capital - 1, "passive_return": passive_eq - 1,
            "n_days": len(days), "n_trades": n_tr, "win_rate": wins / n_tr if n_tr else None,
            "days_waiting": waiting, "last_day": str(days[-1].day) if days else None,
            "stopped_at": ps.stopped_at.isoformat(timespec="seconds") if ps.stopped_at else None,
            "equity_curve": [{"time": int(start.timestamp()) - 86400, "value": ps.capital}] +
                            [{"time": int(pd.Timestamp(d.day, tz="UTC").timestamp()), "value": d.equity} for d in days],
            "days": [{"day": str(d.day), "equity": d.equity, "pnl": d.pnl, "trades": len(d.trades),
                      "passive": d.passive} for d in reversed(days[-60:])],
            "last_trades": days[-1].trades if days else []})
