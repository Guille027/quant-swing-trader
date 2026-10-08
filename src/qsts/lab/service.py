"""The library of bots and everything shown about each one. Two kinds of bot:

- a STOCK bot: one strategy on one stock or ETF (qsts.lab.backtest);
- a PORTFOLIO bot: one strategy as a scanner over the S&P 500, at most 5 positions at a time (qsts.lab.portfolio).
  It also tests the strategy on every stock on its own ("por acción"). Its results take a while (hundreds of
  stocks), so they are computed by a background worker, kept in memory and on disk, and recomputed only when the
  strategy's code, the bot's settings or the stored prices change.
"""
from __future__ import annotations

import json
import pickle
import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import select

from qsts.core.hashing import hash_obj
from qsts.core.jsonsafe import clean as _clean
from qsts.data.bars import nyse_sessions
from qsts.db import models as m
from qsts.lab import metrics
from qsts.lab.audit import audit as run_audit
from qsts.lab.audit import audit_portfolio
from qsts.lab.backtest import REASONS, BacktestConfig, BacktestResult, _simulate, buy_and_hold, run_backtest
from qsts.lab.portfolio import (Panel, PanelBuilder, PortfolioConfig, PortfolioResult, breadth_summary, candidates,
                                run_portfolio, snapshot)
from qsts.lab.strategy import MAX_POSITIONS, REGISTRY, load_all

CAPITAL = 10_000.0
UNIVERSE = "SP500"
UNIVERSE_NAME = "S&P 500"


def bot_id(strategy: str, symbol: str, params: dict | None = None, max_positions: int | None = None) -> str:
    base = f"{strategy}-{symbol}".lower()
    if max_positions and max_positions != MAX_POSITIONS:
        base += f"-{max_positions}pos"
    if params:
        tail = "-".join(f"{k}{v}" for k, v in sorted(params.items()))
        base += "-" + re.sub(r"[^a-z0-9._]+", "", tail.lower())[:24]
    return base[:64]


def is_universe(b: m.LabBot) -> bool:
    return b.kind == "universe"


def _points(s: pd.Series, scale: float = 1.0) -> list[dict]:
    return [{"time": int(t.timestamp()), "value": round(float(v) * scale, 4)} for t, v in s.items() if np.isfinite(v)]


class NotReady(Exception):
    """A portfolio bot's results are still being computed (the status says how far it is)."""

    def __init__(self, status: dict):
        super().__init__(status.get("msg", "calculando"))
        self.status = status


@dataclass
class UniverseRun:
    key: str
    result: PortfolioResult
    breadth: list[dict]          # the strategy on each stock on its own
    snapshot: dict               # last sessions' signals of every stock (for the paper trader)
    next_candidates: list[dict]  # entries signalled at the last close, best ranked first
    last_day: pd.Timestamp
    n_symbols: int
    dated: bool                  # True when each stock is only traded from its date added to the index


class LabService:
    def __init__(self, sf, frame: Callable[[str], pd.DataFrame], token: Callable[[str], tuple],
                 basket: Callable[[], list[str]] | None = None, benchmark: str = "SPY",
                 universe: Callable[[], dict] | None = None, data_version: Callable[[], tuple] | None = None,
                 frame_raw: Callable[[str], pd.DataFrame] | None = None, cache_dir: str | Path | None = None):
        self.sf, self.frame, self.token, self.basket, self.benchmark = sf, frame, token, basket, benchmark
        self.universe_source, self.data_version = universe, data_version or (lambda: ())
        self.frame_raw = frame_raw or frame
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._cache: dict[str, tuple] = {}
        self._audits: dict[str, tuple] = {}
        self._runs: dict[str, UniverseRun] = {}
        self._uaudits: dict[str, tuple] = {}
        self._status: dict[str, dict] = {}
        self._queue: queue.Queue = queue.Queue()
        self._queued: set[tuple] = set()
        self._worker: threading.Thread | None = None
        self._lock = threading.RLock()
        load_all()

    # ------------------------------------------------------------------ bots
    def sync(self) -> int:
        """Create the default bots of every strategy (a bot the user removed is not created again): one per default
        stock and, for strategies meant for many stocks, the S&P 500 portfolio bot."""
        added = 0
        with self.sf() as s, s.begin():
            have = {b.id for b in s.scalars(select(m.LabBot))}
            for key, st in sorted(REGISTRY.items()):
                for sym in st.default_symbols:
                    bid = bot_id(key, sym)
                    if bid not in have:
                        s.add(m.LabBot(id=bid, strategy=key, symbol=sym, params={}))
                        added += 1
                bid = bot_id(key, UNIVERSE)
                if st.universe and self.universe_source is not None and bid not in have:
                    s.add(m.LabBot(id=bid, strategy=key, symbol=UNIVERSE, params={}, kind="universe",
                                   max_positions=st.positions()))
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

    def create_bot(self, strategy: str, symbol: str, params: dict | None = None,
                   max_positions: int | None = None) -> m.LabBot:
        if strategy not in REGISTRY:
            raise KeyError(strategy)
        symbol = symbol.strip().upper().replace(".", "-")
        universe = symbol in (UNIVERSE, "S&P500", "S&P-500", "SP-500")
        if universe:
            symbol = UNIVERSE
            max_positions = int(max_positions or REGISTRY[strategy].positions())
            if not 1 <= max_positions <= MAX_POSITIONS:
                raise ValueError(f"las posiciones a la vez deben estar entre 1 y {MAX_POSITIONS}")
        elif not re.fullmatch(r"[A-Z0-9\-]{1,12}", symbol):
            raise ValueError("símbolo no válido")
        params = {k: v for k, v in (params or {}).items() if REGISTRY[strategy].params.get(k) != v}
        REGISTRY[strategy].resolve(params)  # unknown parameters are refused
        bid = bot_id(strategy, symbol, params, max_positions if universe else None)
        with self.sf() as s, s.begin():
            b = s.get(m.LabBot, bid)
            if b is None:
                b = m.LabBot(id=bid, strategy=strategy, symbol=symbol, params=params,
                             kind="universe" if universe else None, max_positions=max_positions if universe else None)
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

    # ------------------------------------------------------------------ stock bots
    def config(self, b: m.LabBot) -> BacktestConfig:
        return BacktestConfig(initial_capital=CAPITAL, size_pct=b.size_pct or 100.0)

    def _key(self, b: m.LabBot) -> str:
        st = REGISTRY[b.strategy]
        return json.dumps([b.id, st.version(), b.params or {}, b.size_pct, list(map(str, self.token(b.symbol)))],
                          sort_keys=True, default=str)

    def result(self, b: m.LabBot) -> tuple[BacktestResult, pd.DataFrame]:
        if is_universe(b):
            raise ValueError("es un bot de cartera")
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

    # ------------------------------------------------------------------ portfolio bots: universe and worker
    def universe(self) -> dict:
        """{"symbols": {symbol: date added or None}, "dated": bool, "last": {symbol: last bar}} (stocks with prices)."""
        if self.universe_source is None:
            return {"symbols": {}, "dated": False, "last": {}}
        return self.universe_source()

    def pconfig(self, b: m.LabBot) -> PortfolioConfig:
        return PortfolioConfig(initial_capital=CAPITAL, max_positions=b.max_positions or MAX_POSITIONS)

    def _ukey(self, b: m.LabBot, uni: dict) -> str:
        st = REGISTRY[b.strategy]
        members = hash_obj(sorted((s, str(d)) for s, d in uni["symbols"].items()), 16)
        return json.dumps([b.id, st.version(), b.params or {}, b.max_positions, list(map(str, self.data_version())),
                           members], sort_keys=True, default=str)

    def status(self, bid: str) -> dict | None:
        with self._lock:
            st = self._status.get(bid)
            return dict(st) if st else None

    def _set_status(self, bid: str, **kw) -> None:
        with self._lock:
            self._status.setdefault(bid, {}).update(kw)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, daemon=True, name="lab-worker")
                self._worker.start()

    def _schedule(self, bid: str, what: str) -> None:
        with self._lock:
            if (bid, what) in self._queued:
                return
            self._queued.add((bid, what))
            self._status[bid] = {"what": what, "state": "en cola", "msg": "en cola", "done": 0, "total": 0}
        self._queue.put((bid, what))
        self._ensure_worker()

    def _work(self) -> None:
        while True:
            bid, what = self._queue.get()
            try:
                b = self.get_bot(bid)
                if what == "run":
                    self._compute_run(b)
                else:
                    self._compute_audit(b)
                with self._lock:
                    self._status.pop(bid, None)
            except Exception as e:  # noqa: BLE001 - shown on the bot's row; the worker keeps going
                self._set_status(bid, state="error", msg=f"error: {e}"[:300])
            finally:
                with self._lock:
                    self._queued.discard((bid, what))

    def wait(self, timeout: float = 600.0) -> None:
        """Tests and the CLI: block until the worker has nothing left."""
        import time
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                busy = bool(self._queued)
            if not busy:
                return
            time.sleep(0.05)
        raise TimeoutError("the lab worker did not finish")

    def _disk(self, name: str) -> Path | None:
        if self.cache_dir is None:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / f"{re.sub(r'[^a-z0-9._-]+', '_', name.lower())}.pkl"

    def _load_disk(self, name: str, key: str):
        p = self._disk(name)
        if p is None or not p.exists():
            return None
        try:
            with open(p, "rb") as f:
                k, v = pickle.load(f)
            return v if k == key else None
        except Exception:  # noqa: BLE001 - a broken cache file is just recomputed
            return None

    def _save_disk(self, name: str, key: str, value) -> None:
        p = self._disk(name)
        if p is None:
            return
        try:
            tmp = p.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                pickle.dump((key, value), f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(p)
        except OSError:
            pass

    def urun(self, b: m.LabBot, schedule: bool = True) -> UniverseRun:
        """The portfolio bot's results, or NotReady (and the computation is started in the background)."""
        uni = self.universe()
        if not uni["symbols"]:
            raise KeyError(UNIVERSE)
        key = self._ukey(b, uni)
        with self._lock:
            run = self._runs.get(b.id)
        if run is not None and run.key == key:
            return run
        run = self._load_disk(f"run-{b.id}", key)
        if run is not None:
            with self._lock:
                self._runs[b.id] = run
            return run
        if schedule:
            self._schedule(b.id, "run")
        raise NotReady(self.status(b.id) or {"state": "en cola", "msg": "en cola"})

    def _calendar(self, uni: dict) -> pd.DatetimeIndex:
        firsts = [v for v in uni.get("first", {}).values() if v is not None]
        lasts = [v for v in uni.get("last", {}).values() if v is not None]
        return nyse_sessions(min(firsts), max(lasts))

    def build_panel(self, b: m.LabBot, params: dict | None = None, frames: dict | None = None,
                    breadth: list | None = None, label: str = "calculando") -> Panel:
        """Loads every stock of the universe (one at a time), computes its signals and fills the panel. With
        `breadth` (a list), also backtests the strategy on each stock on its own."""
        st = REGISTRY[b.strategy]
        uni = self.universe()
        syms = sorted(uni["symbols"])
        pb = PanelBuilder(self._calendar(uni), syms)
        p = (b.params or {}) if params is None else params
        for n, sym in enumerate(syms):
            if n % 10 == 0:
                self._set_status(b.id, state="calculando", msg=f"{label}: {n} de {len(syms)} acciones", done=n,
                                 total=len(syms))
            try:
                bars = frames[sym] if frames is not None and sym in frames else self.frame_raw(sym)
            except (KeyError, ValueError):
                continue
            if frames is not None and sym not in frames:
                frames[sym] = bars
            sig = st.run(bars, p)
            start = uni["symbols"][sym]
            start = pd.Timestamp(start).tz_localize("UTC") if start is not None and pd.Timestamp(start).tz is None \
                else (pd.Timestamp(start) if start is not None else None)
            pb.add(sym, bars, sig, start)
            if breadth is not None:
                breadth.append(self._one_stock(sym, bars, sig, start))
        return pb.done()

    @staticmethod
    def _one_stock(sym: str, bars: pd.DataFrame, sig: pd.DataFrame, start) -> dict:
        window = np.ones(len(bars), bool) if start is None else np.asarray(bars.index >= start)
        res = _simulate(bars, sig, window, BacktestConfig(initial_capital=CAPITAL))
        ts = metrics.trade_stats(res.trades)
        eq = res.equity
        hold = buy_and_hold(bars, eq.index[0], eq.index[-1], CAPITAL) if len(eq) else eq
        return {"symbol": sym, "net": float(eq.iloc[-1] / CAPITAL - 1) if len(eq) else 0.0,
                "hold": float(hold.iloc[-1] / CAPITAL - 1) if len(hold) else None,
                "trades": ts["n_trades"], "win_rate": ts["win_rate"], "profit_factor": ts["profit_factor"],
                "wins": ts["n_wins"], "gross_profit": ts["gross_profit"], "gross_loss": ts["gross_loss"],
                "avg_trade_pct": ts["avg_trade_pct"], "max_drawdown": float(metrics.drawdown(eq).min()) if len(eq) else None,
                "since": str(eq.index[0].date()) if len(eq) else None}

    def _compute_run(self, b: m.LabBot) -> UniverseRun:
        uni = self.universe()
        key = self._ukey(b, uni)
        breadth: list[dict] = []
        panel = self.build_panel(b, breadth=breadth, label="probando la estrategia")
        self._set_status(b.id, state="calculando", msg="simulando la cartera día a día")
        res = run_portfolio(panel, self.pconfig(b))
        last = len(panel.index) - 1
        while last > 0 and not np.isfinite(panel.c[last]).any():
            last -= 1
        run = UniverseRun(key=key, result=res, breadth=breadth, snapshot=snapshot(panel),
                          next_candidates=candidates(panel, last), last_day=panel.index[last],
                          n_symbols=len(panel.symbols), dated=bool(uni.get("dated")))
        with self._lock:
            self._runs[b.id] = run
        self._save_disk(f"run-{b.id}", key, run)
        return run

    def _compute_audit(self, b: m.LabBot) -> dict:
        uni = self.universe()
        key = self._ukey(b, uni)
        frames: dict = {}
        panel = self.build_panel(b, frames=frames, label="preparando la auditoría")
        run = self._runs.get(b.id)
        if run is None or run.key != key:
            run = self._compute_run(b)
        st = REGISTRY[b.strategy]
        bench = self.frame(self.benchmark)["close"] if self.benchmark else pd.Series(dtype=float)

        def rebuild(params):
            return self.build_panel(b, params=params, frames=frames, label="probando variantes de los ajustes")
        out = _clean(audit_portfolio(panel, self.pconfig(b), bench, breadth_summary(run.breadth), self.n_tested(),
                                     rebuild=rebuild, strategy=st, params=b.params or {},
                                     progress=lambda msg: self._set_status(b.id, state="auditando",
                                                                           msg=f"auditando: {msg}")))
        with self._lock:
            self._uaudits[b.id] = (key, out)
        self._save_disk(f"audit-{b.id}", key, out)
        return out

    # ------------------------------------------------------------------ rows, detail, audit
    def row(self, b: m.LabBot) -> dict:
        st = REGISTRY[b.strategy]
        uni = is_universe(b)
        base = {"id": b.id, "strategy": b.strategy, "name": st.name, "symbol": UNIVERSE_NAME if uni else b.symbol,
                "kind": "universe" if uni else "stock", "max_positions": b.max_positions if uni else None,
                "timeframe": "1 día", "favorite": bool(b.favorite), "paper_status": b.paper_status,
                "allow_short": st.allow_short, "custom": bool(b.params)}
        try:
            if uni:
                run = self.urun(b)
                eq, tr, pos = run.result.equity, run.result.trades, run.result.position
                extra = {"in_position": bool(run.result.open_trades), "n_symbols": run.n_symbols,
                         "breadth": breadth_summary(run.breadth).get("profitable")}
            else:
                res, _bars = self.result(b)
                eq, tr, pos = res.equity, res.trades, res.position
                extra = {"in_position": res.open_trade is not None}
        except NotReady as e:
            return {**base, "computing": e.status}
        except (KeyError, ValueError) as e:
            if uni and isinstance(e, KeyError):
                return {**base, "error": "faltan los precios del S&P 500: descárgalos en Datos", "need_universe": True}
            return {**base, "error": f"sin datos de {b.symbol}: descárgalos en Datos" if isinstance(e, KeyError)
                    else str(e)[:200]}
        sm = metrics.summary(eq, tr, CAPITAL, pos)
        return _clean({**base, **sm, "spark": metrics.sparkline(eq), **extra})

    def library(self) -> dict:
        rows = [self.row(b) for b in self.bots()]
        uni = self.universe()
        return _clean({"rows": rows, "n_bots": len(rows), "n_strategies": len(REGISTRY), "n_tested": self.n_tested(),
                       "strategies": [REGISTRY[k].info() for k in sorted(REGISTRY)],
                       "universe": {"n_symbols": len(uni["symbols"]), "dated": bool(uni.get("dated"))}})

    def _common(self, b, eq, tr, pos, hold) -> dict:
        st = REGISTRY[b.strategy]
        trades = []
        for t in tr.iloc[::-1].head(1000).itertuples(index=False):
            trades.append({"symbol": getattr(t, "symbol", b.symbol), "side": t.side, "entry": str(t.entry_time.date()),
                           "exit": str(t.exit_time.date()), "entry_price": t.entry_price, "exit_price": t.exit_price,
                           "qty": t.qty, "pnl": t.pnl, "pnl_pct": t.pnl_pct, "bars": int(t.bars),
                           "reason": REASONS.get(t.reason, t.reason)})
        return {
            "bot": {"id": b.id, "strategy": st.info(), "symbol": UNIVERSE_NAME if is_universe(b) else b.symbol,
                    "kind": "universe" if is_universe(b) else "stock", "max_positions": b.max_positions,
                    "params": st.resolve(b.params or {}), "custom_params": b.params or {}, "size_pct": b.size_pct,
                    "favorite": bool(b.favorite), "paper_status": b.paper_status, "allocation_pct": b.allocation_pct,
                    "capital": b.capital, "activated_at": b.activated_at.isoformat() if b.activated_at else None},
            "capital": CAPITAL,
            "summary": metrics.summary(eq, tr, CAPITAL, pos),
            "key_metrics": metrics.key_metrics(eq, tr),
            "by_side": metrics.report_by_side(tr, CAPITAL),
            "monthly": metrics.monthly_returns(eq), "weekday": metrics.weekday_exposure(pos),
            "yearly": metrics.yearly(eq, hold),
            "equity": _points(eq / CAPITAL - 1, 100), "hold": _points(hold / CAPITAL - 1, 100),
            "drawdown": _points(metrics.drawdown(eq), 100),
            "pnl_range": {"strategy": metrics.pnl_range(eq, CAPITAL), "hold": metrics.pnl_range(hold, CAPITAL)},
            "trades": trades, "n_trades": int(len(tr)), "monte_carlo": metrics.monte_carlo(tr)}

    def detail(self, bid: str) -> dict:
        b = self.get_bot(bid)
        if is_universe(b):
            return self._udetail(b)
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
        ot = res.open_trade
        return _clean({
            **self._common(b, eq, tr, res.position, hold),
            "config": {"size_pct": res.config.size_pct, "slippage_bps": res.config.slippage_bps,
                       "commission_pct": res.config.commission_pct},
            "open_trade": None if ot is None else {
                "side": ot["side"], "entry": str(ot["entry_time"].date()), "entry_price": ot["entry_price"],
                "price": ot["exit_price"], "pnl_pct": ot["pnl_pct"], "stop": ot.get("stop"), "target": ot.get("target")},
            "next_action": nxt, "last_bar": str(bars.index[-1].date())})

    def _udetail(self, b: m.LabBot) -> dict:
        try:
            run = self.urun(b)
        except NotReady as e:
            st = REGISTRY[b.strategy]
            return _clean({"computing": e.status, "bot": {"id": b.id, "strategy": st.info(), "symbol": UNIVERSE_NAME,
                                                          "kind": "universe", "max_positions": b.max_positions,
                                                          "paper_status": b.paper_status}})
        res = run.result
        eq, tr = res.equity, res.trades
        try:
            bench = self.frame(self.benchmark)
            hold = buy_and_hold(bench, eq.index[0], eq.index[-1], CAPITAL) if len(eq) else eq
        except (KeyError, ValueError):
            hold = pd.Series(dtype=float)
        cfg = res.config
        held = {t["symbol"]: t for t in res.open_trades}
        last = str(run.last_day.date())
        exits = []
        for sym, t in held.items():
            row = run.snapshot.get(sym, {}).get(last)
            if row and ((t["direction"] > 0 and row["exit_long"]) or (t["direction"] < 0 and row["exit_short"])
                        or row["entry"] == -t["direction"]):
                exits.append(sym)
        free = cfg.max_positions - len(held) + len(exits)
        cands = [c for c in run.next_candidates if c["symbol"] not in held]
        positions = [{"symbol": t["symbol"], "side": t["side"], "entry": str(t["entry_time"].date()),
                      "entry_price": t["entry_price"], "price": t["exit_price"], "pnl_pct": t["pnl_pct"],
                      "stop": t.get("stop"), "target": t.get("target"), "exit_next": t["symbol"] in exits}
                     for t in res.open_trades]
        rows = sorted(run.breadth, key=lambda r: -(r["net"] or 0))
        return _clean({
            **self._common(b, eq, tr, res.position, hold),
            "config": {"max_positions": cfg.max_positions, "slippage_bps": cfg.slippage_bps,
                       "commission_pct": cfg.commission_pct, "slot_pct": 100 / cfg.max_positions},
            "positions": positions, "avg_positions": float(res.position.mean()) if len(res.position) else None,
            "next": {"day": last, "exits": exits, "free": max(free, 0),
                     "buys": [{**c, "chosen": n < max(free, 0)} for n, c in enumerate(cands[:15])],
                     "n_signals": len(cands)},
            "breadth": {"summary": breadth_summary(run.breadth), "rows": rows},
            "universe": {"n_symbols": run.n_symbols, "dated": run.dated, "name": UNIVERSE_NAME},
            "open_trade": None, "next_action": None, "last_bar": last})

    def audit(self, bid: str) -> dict:
        b = self.get_bot(bid)
        if is_universe(b):
            return self._uaudit(b)
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

    def _uaudit(self, b: m.LabBot) -> dict:
        uni = self.universe()
        if not uni["symbols"]:
            raise KeyError(UNIVERSE)
        key = self._ukey(b, uni)
        with self._lock:
            hit = self._uaudits.get(b.id)
        if hit is not None and hit[0] == key:
            return hit[1]
        out = self._load_disk(f"audit-{b.id}", key)
        if out is not None:
            with self._lock:
                self._uaudits[b.id] = (key, out)
            return out
        self._schedule(b.id, "audit")
        return {"computing": self.status(b.id)}

    # ------------------------------------------------------------------ for the paper trader
    def signal_view(self, b: m.LabBot, due: pd.Timestamp) -> dict | None:
        """Signals of the close of `due` for every stock the bot may trade: {"rows": {symbol: row}, "ranked":
        [entry candidates, best first], "max_positions": n}. None while a portfolio bot is being computed.
        A stock without a bar on `due` has no row (nothing is decided on old prices)."""
        day = str(pd.Timestamp(due).date())
        if is_universe(b):
            try:
                run = self.urun(b)
            except NotReady:
                return None
            rows = {s: r[day] for s, r in run.snapshot.items() if day in r}
            ranked = sorted((s for s, r in rows.items() if r["entry"] != 0 and r["eligible"]),
                            key=lambda s: (-rows[s]["rank"] if np.isfinite(rows[s]["rank"]) else np.inf, s))
            return {"rows": rows, "ranked": ranked, "max_positions": b.max_positions or MAX_POSITIONS,
                    "size": 1 / (b.max_positions or MAX_POSITIONS), "last_day": run.last_day}
        res, bars = self.result(b)
        t = pd.Timestamp(day, tz="UTC")
        rows = {}
        if t in res.signals.index:
            s = res.signals.loc[t]
            rows[b.symbol] = {"close": float(bars["close"].loc[t]), "entry": int(s["entry"]),
                              "exit_long": bool(s["exit_long"]), "exit_short": bool(s["exit_short"]),
                              "rank": float(s["rank"]), "eligible": True,
                              **{k: float(s[k]) for k in ("stop", "target", "stop_pct", "target_pct", "trail")}}
        ranked = [b.symbol] if rows and rows[b.symbol]["entry"] != 0 else []
        return {"rows": rows, "ranked": ranked, "max_positions": 1, "size": (b.size_pct or 100.0) / 100,
                "last_day": bars.index[-1]}

    def stale_symbols(self, b: m.LabBot, due: pd.Timestamp) -> list[str]:
        """Stocks the bot needs whose last stored bar is older than `due`."""
        if is_universe(b):
            uni = self.universe()
            return sorted(s for s, t in uni.get("last", {}).items() if t is None or pd.Timestamp(t) < due)
        try:
            bars = self.frame(b.symbol)
        except (KeyError, ValueError):
            return [b.symbol]
        return [b.symbol] if bars.index[-1] < due else []

    def open_trades(self, b: m.LabBot) -> list[dict]:
        """The backtest's open positions now (followed at activation)."""
        if is_universe(b):  # not those the last close already tells to sell
            run = self.urun(b)
            last = str(run.last_day.date())
            out = []
            for t in run.result.open_trades:
                row = run.snapshot.get(t["symbol"], {}).get(last)
                d = t["direction"]
                if row and ((d > 0 and row["exit_long"]) or (d < 0 and row["exit_short"]) or row["entry"] == -d):
                    continue
                out.append(t)
            return out
        res, _ = self.result(b)
        return [{**res.open_trade, "symbol": b.symbol}] if res.open_trade is not None else []
