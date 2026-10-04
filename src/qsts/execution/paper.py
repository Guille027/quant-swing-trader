"""Paper trading ("Simulación"): one frozen strategy version run FORWARD on new data, with fictitious money.

It reuses the backtest engine unchanged (same signals at the close, next-open fills, stops/targets, costs and
risk sizing), run from the session's start bar with `close_at_end=False`, so paper results are directly
comparable with research. The engine is causal, so recomputing with more data does not change past days;
every evening's state is journaled (PaperDay) and any later difference (e.g. a vendor data revision) is
reported, never hidden. A session can only start from TODAY's data (no back-dated "paper" results) and only
for a strategy that passed its final out-of-sample test (lifecycle CANDIDATE -> PAPER, by a human).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import select

from qsts.backtest.benchmarks import buy_and_hold
from qsts.backtest.engine import BacktestConfig, BacktestEngine
from qsts.data.bars import nyse_schedule
from qsts.db import models as m
from qsts.research.experiments import _clean, config_from_dict
from qsts.strategy.definition import definition_from_dict
from qsts.strategy.lifecycle import Status, StrategyRegistry

MAX_STALE_SESSIONS = 3
REVISION_TOLERANCE = 1e-4  # relative; backward price adjustment leaves $ results unchanged up to float noise


class PaperError(ValueError):
    pass


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def last_completed_session(asof=None) -> pd.Timestamp:
    now = _utc(asof) if asof is not None else pd.Timestamp.now(tz="UTC")
    sched = nyse_schedule(now - pd.Timedelta(days=14), now)
    return sched.index[sched["market_close"] <= now][-1]


def next_open(after_day: pd.Timestamp) -> pd.Timestamp | None:
    sched = nyse_schedule(after_day + pd.Timedelta(days=1), after_day + pd.Timedelta(days=14))
    return sched["market_open"].iloc[0] if len(sched) else None


class PaperTrading:
    def __init__(self, sf, load_frame: Callable[[str], pd.DataFrame], benchmark: str = "SPY"):
        self.sf, self.load_frame, self.benchmark = sf, load_frame, benchmark
        self.registry = StrategyRegistry(sf)

    # -------------------------------------------------------------- sessions
    def active(self) -> m.PaperSession | None:
        with self.sf() as s:
            return s.scalars(select(m.PaperSession).where(m.PaperSession.status == "ACTIVE")
                             .order_by(m.PaperSession.id.desc())).first()

    def candidates(self) -> list[dict]:
        """Strategies allowed to start paper trading (passed the final test -> CANDIDATE)."""
        from qsts.research.autoresearch import describe
        out = []
        with self.sf() as s:
            for st in s.scalars(select(m.Strategy).where(m.Strategy.status == Status.CANDIDATE.value)).all():
                v = s.scalars(select(m.StrategyVersion).where(m.StrategyVersion.strategy_id == st.id)
                              .order_by(m.StrategyVersion.version.desc())).first()
                if v is None:
                    continue
                rc = s.scalars(select(m.ResearchCandidate).where(m.ResearchCandidate.strategy_id == st.id)).first()
                out.append({"strategy_id": st.id, "rules": describe(definition_from_dict(v.definition)),
                            "consistency": rc.fitness if rc else None, "final": rc.final if rc else None})
        return out

    def _research_setup(self, version_id: str) -> tuple[list[str], dict]:
        """Symbols and engine config the strategy was validated with (paper must use the same rules)."""
        with self.sf() as s:
            e = s.scalars(select(m.Experiment).where(m.Experiment.strategy_version_id == version_id)
                          .order_by(m.Experiment.created_at.desc())).first()
        if e is None or not e.config.get("symbols"):
            raise PaperError("no sé con qué acciones se validó esta estrategia")
        return list(e.config["symbols"]), dict(e.config.get("engine") or {})

    def _load(self, symbols: list[str], until=None) -> dict[str, pd.DataFrame]:
        data = {}
        for sym in symbols:
            try:
                df = self.load_frame(sym)
            except Exception:  # noqa: BLE001 - a missing/invalid symbol is skipped (reported in the view)
                continue
            if until is not None:
                df = df[df.index <= _utc(until)]
            if len(df):
                data[sym] = df
        return data

    def start(self, strategy_id: str, capital: float = 10_000.0, *, asof=None, actor: str = "user") -> int:
        if self.active() is not None:
            raise PaperError("ya hay una simulación en marcha: detenla antes de empezar otra")
        if capital <= 0:
            raise PaperError("el capital debe ser positivo")
        with self.sf() as s:
            if s.get(m.Strategy, strategy_id) is None:
                raise PaperError(f"estrategia desconocida: {strategy_id}")
        if self.registry.status(strategy_id) is not Status.CANDIDATE:
            raise PaperError("solo se pueden simular estrategias aprobadas en el test final (estado CANDIDATE)")
        with self.sf() as s:
            v = s.scalars(select(m.StrategyVersion).where(m.StrategyVersion.strategy_id == strategy_id)
                          .order_by(m.StrategyVersion.version.desc())).first()
        symbols, engine = self._research_setup(v.id)
        data = self._load(symbols, until=asof)
        if not data:
            raise PaperError("no hay datos para las acciones de esta estrategia")
        last_bar = max(df.index.max() for df in data.values())
        due = last_completed_session(asof)
        behind = len(nyse_schedule(last_bar + pd.Timedelta(days=1), due)) if last_bar < due else 0
        if behind > MAX_STALE_SESSIONS:
            raise PaperError(f"los precios llevan {behind} sesiones sin actualizar: actualízalos antes de empezar")
        cfg = config_from_dict({**engine, "initial_capital": float(capital)}) if engine else \
            BacktestConfig(initial_capital=float(capital))
        self.registry.transition(strategy_id, Status.PAPER, actor=actor,
                                 reason=f"paper trading started with {capital:.0f} (fictitious)")
        with self.sf() as s, s.begin():
            ps = m.PaperSession(strategy_id=strategy_id, version_id=v.id, symbols=sorted(data), start=last_bar.date(),
                                capital=float(capital), config=cfg.to_dict(), status="ACTIVE")
            s.add(ps)
            s.flush()
            return ps.id

    def stop(self, reason: str = "detenida por el usuario", actor: str = "user") -> None:
        ps = self.active()
        if ps is None:
            raise PaperError("no hay ninguna simulación en marcha")
        with self.sf() as s, s.begin():
            row = s.get(m.PaperSession, ps.id)
            row.status, row.stop_reason = "STOPPED", reason
            row.stopped_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.registry.transition(ps.strategy_id, Status.UNDER_REVIEW, actor=actor, reason=f"paper stopped: {reason}")

    # -------------------------------------------------------------- the daily view
    def view(self, *, until=None, asof=None) -> dict:
        """Recompute the session up to the latest stored bar (or `until`, for tests) and journal new days."""
        ps = self.active()
        if ps is None:
            return {"active": False, "candidates": self.candidates()}
        from qsts.research.autoresearch import describe
        with self.sf() as s:
            v = s.get(m.StrategyVersion, ps.version_id)
        sd = definition_from_dict(v.definition)
        cfg = config_from_dict(ps.config)
        data = self._load(ps.symbols, until=until)
        start = pd.Timestamp(ps.start, tz="UTC")
        res = BacktestEngine(cfg).run(sd, data, start=start, close_at_end=False)
        eq = res.equity
        last_bar = eq.index[-1]
        due = last_completed_session(asof if asof is not None else until)
        stale = len(nyse_schedule(last_bar + pd.Timedelta(days=1), due)) if last_bar < due else 0
        nxt = next_open(last_bar)
        def days_to(sym):
            df = data.get(sym)
            if df is None or "earn_days_to" not in df:
                return None
            v = float(df["earn_days_to"].iloc[-1])
            return v if np.isfinite(v) and v <= 10 else None
        orders = [{"action": "VENDER", "symbol": p["symbol"], "qty": p["qty"], "reason": p["pending_exit"],
                   "approx_value": p["market_value"]} for p in res.open_positions if p["pending_exit"]]
        # buys are filled in decision order while cash lasts (sells at the same open free cash first)
        cash_left = float(eq["cash"].iloc[-1]) + sum(o["approx_value"] for o in orders)
        for o in res.pending_orders:
            c = o["last_close"]
            want = o["qty"] * c * (1 + (cfg.costs.spread_bps / 2 + cfg.costs.slippage_bps) / 1e4)
            fit = max(min(want, cash_left), 0.0)
            cash_left -= fit
            orders.append({"action": "COMPRAR", "symbol": o["symbol"], "qty": o["qty"] * (fit / want if want else 0),
                           "approx_value": fit, "last_close": c, "approx_stop": c - o["stop_dist"],
                           "approx_target": c + o["tp_dist"] if np.isfinite(o["tp_dist"]) else None,
                           "likely": fit > 0.01 * want, "partial": 0 < fit < 0.99 * want,
                           "earnings_in": days_to(o["symbol"])})
        revisions = self._journal(ps.id, eq, orders)
        trades = res.trades
        closed = [] if trades.empty else [
            {"symbol": t.symbol, "entry": str(t.entry_ts)[:10], "exit": str(t.exit_ts)[:10], "qty": float(t.qty),
             "entry_price": float(t.entry_price), "exit_price": float(t.exit_price), "pnl": float(t.pnl),
             "reason": t.exit_reason} for t in trades.sort_values("exit_ts").itertuples(index=False)]
        equity_now = float(eq["equity"].iloc[-1])
        out = {"active": True, "session": {"id": ps.id, "strategy_id": ps.strategy_id, "rules": describe(sd),
                                           "start": str(ps.start), "capital": ps.capital, "n_symbols": len(ps.symbols)},
               "as_of": str(last_bar.date()), "stale_sessions": stale,
               "next_open": nxt.isoformat() if nxt is not None else None,
               "equity": equity_now, "cash": float(eq["cash"].iloc[-1]), "pnl": equity_now - ps.capital,
               "return": equity_now / ps.capital - 1, "days": int(len(eq) - 1),
               "n_closed": len(closed), "win_rate": float(np.mean([t["pnl"] > 0 for t in closed])) if closed else None,
               "positions": [{**p, "entry_ts": str(p["entry_ts"])[:10], "earnings_in": days_to(p["symbol"])}
                             for p in res.open_positions],
               "earnings_rule": {"blackout_days": cfg.earnings_blackout_days, "exit_before": cfg.exit_before_earnings},
               "orders": orders, "closed": closed[-200:], "revisions": revisions,
               "missing_symbols": sorted(set(ps.symbols) - set(data)),
               "curve": [{"time": int(t.timestamp()), "value": float(x)} for t, x in eq["equity"].items()]}
        try:
            b = self.load_frame(self.benchmark)["close"]
            b = b[(b.index >= start) & (b.index <= last_bar)]
            if len(b) > 1:
                beq = buy_and_hold(b, cfg)["equity"]
                out["benchmark"] = {"return": float(beq.iloc[-1] / cfg.initial_capital - 1),
                                    "curve": [{"time": int(t.timestamp()), "value": float(x)} for t, x in beq.items()]}
        except Exception:  # noqa: BLE001 - the comparison is optional
            pass
        return _clean(out)

    def _journal(self, session_id: int, eq: pd.DataFrame, orders: list[dict]) -> dict:
        """Append new days; compare already-journaled days with the recomputation (reported, never rewritten)."""
        with self.sf() as s, s.begin():
            stored = {r.day: r for r in s.scalars(select(m.PaperDay).where(m.PaperDay.session_id == session_id))}
            changed, worst = 0, 0.0
            last_day = eq.index[-1].date()
            for ts, row in eq.iterrows():
                d = ts.date()
                if d in stored:
                    old = stored[d].equity
                    diff = abs(row["equity"] - old) / max(abs(old), 1e-9)
                    if diff > REVISION_TOLERANCE:
                        changed, worst = changed + 1, max(worst, diff)
                    continue
                s.add(m.PaperDay(session_id=session_id, day=d, equity=float(row["equity"]), cash=float(row["cash"]),
                                 n_positions=int(row["n_positions"]),
                                 orders=_clean(orders) if d == last_day else None))
        return {"days_changed": changed, "max_relative_change": worst}

    def summary(self) -> dict:
        """Cheap status for the home screen (no recomputation)."""
        ps = self.active()
        if ps is None:
            with self.sf() as s:
                n = len(s.scalars(select(m.Strategy.id).where(m.Strategy.status == Status.CANDIDATE.value)).all())
            return {"active": False, "candidates": n}
        with self.sf() as s:
            last = s.scalars(select(m.PaperDay).where(m.PaperDay.session_id == ps.id)
                             .order_by(m.PaperDay.day.desc())).first()
        eq = last.equity if last else ps.capital
        return {"active": True, "start": str(ps.start), "capital": ps.capital, "equity": eq,
                "return": eq / ps.capital - 1, "last_day": str(last.day) if last else None}

    def journal(self) -> list[dict]:
        ps = self.active()
        if ps is None:
            return []
        with self.sf() as s:
            rows = s.scalars(select(m.PaperDay).where(m.PaperDay.session_id == ps.id).order_by(m.PaperDay.day)).all()
            return _clean([{"day": str(r.day), "equity": r.equity, "n_positions": r.n_positions, "orders": r.orders,
                            "recorded_at": str(r.recorded_at)[:19]} for r in rows])
