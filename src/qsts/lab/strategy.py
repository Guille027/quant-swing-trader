"""How a strategy is written, and the registry of all strategies.

A strategy is a Python class (one per file in `qsts/lab/strategies/`) that turns daily bars into signals, the way a
TradingView `strategy()` script does. Every signal is decided with the bar's CLOSE and executed at the NEXT bar's
OPEN (TradingView's default), so a signal can only use data up to and including its own bar. This is checked
automatically for every registered strategy (tests/test_lab.py::test_every_strategy_is_causal).

`signals(bars, params)` returns a DataFrame on the same index with:
- `entry`: +1 open a long, -1 open a short, 0 nothing (ignored while a position in that direction is open)
- `exit`: True closes the open position (long or short); or, for strategies with both sides,
  `exit_long` / `exit_short`: True closes only a position in that direction
- optional `stop` / `target`: price levels for a position opened by this bar's signal
- optional `stop_pct` / `target_pct`: the same, as a fraction of the entry fill price (0.05 = 5%)
- optional `trail`: a stop level that can only move in the position's favour (applies from the next bar)
- optional `entry_stop`: instead of buying at the open, a STOP order at this level for the next session (long: fills
  when the price rises to it, at the open if it gaps through; short: mirror). Not reached = no trade.
- optional `entry_limit`: a LIMIT-ON-OPEN order: fills at the next open only if the open is at or better than the
  level (long: open <= level). Used for gap strategies ("buy only if it opens 1% lower").

`day_trade = True`: every position is closed at the close of the day it was opened (market-on-close): intraday
strategies on daily bars. Holding time is then 0 sessions.

Portfolio mode (the strategy run as a scanner over the S&P 500, at most `max_positions` at a time): when more
stocks signal than there are free places, the ones with the highest `rank` are taken. `rank(bars, params)` is the
author's ranking rule when the source gives one (describe it in `rank_rule`); otherwise the fixed rule applies:
the most liquid stock first (highest average traded value of the last 20 sessions). Both are causal.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import pkgutil
from typing import ClassVar

import numpy as np
import pandas as pd

SIGNAL_COLUMNS = ("entry", "exit", "exit_long", "exit_short", "stop", "target", "stop_pct", "target_pct", "trail",
                  "entry_stop", "entry_limit")
FLOAT_COLUMNS = ("stop", "target", "stop_pct", "target_pct", "trail", "entry_stop", "entry_limit")
MAX_POSITIONS = 5  # the user's limit for a portfolio bot
DEFAULT_RANK_RULE = "la más líquida primero (mayor volumen negociado en dólares, media de 20 sesiones)"


def liquidity_rank(bars: pd.DataFrame) -> pd.Series:
    """The fixed ranking rule: average traded value (close x volume) of the last 20 sessions, this one included."""
    return (bars["close"] * bars["volume"]).rolling(20, min_periods=20).mean()


class Strategy:
    key: ClassVar[str]                     # unique id, never changes (bots refer to it)
    name: ClassVar[str]                    # shown in the library
    source: ClassVar[str] = ""             # where it comes from (TradingView script, video, book, author)
    summary: ClassVar[str] = ""            # the rules in plain Spanish
    default_symbols: ClassVar[tuple[str, ...]] = ("SPY",)  # bots created when the strategy is added
    params: ClassVar[dict] = {}            # default parameters (as published)
    param_grid: ClassVar[dict] = {}        # nearby values tried by the robustness check
    allow_short: ClassVar[bool] = False    # True if the strategy also opens shorts
    warmup: ClassVar[int] = 0              # bars needed before the first valid signal (informative)
    universe: ClassVar[bool] = True        # also run it as an S&P 500 portfolio (False: single-asset strategy)
    max_positions: ClassVar[int | None] = None  # the author's number of positions, if published (capped at 5)
    rank_rule: ClassVar[str] = ""          # the author's ranking rule in plain Spanish ("" = the fixed rule)
    day_trade: ClassVar[bool] = False      # positions closed at the close of their entry day (intraday)
    overnight: ClassVar[bool] = False      # bought at the CLOSE (signal of the day before), sold at the next open
    style: ClassVar[str] = ""              # "intradía" | "pocos días" | "semanas" | "meses" (as published)
    notes: ClassVar[str] = ""              # how the published rules were interpreted for this app

    def rank(self, bars: pd.DataFrame, p: dict) -> pd.Series | None:
        """The author's ranking (higher = taken first); None = the fixed rule (most liquid first)."""
        return None

    def signals(self, bars: pd.DataFrame, p: dict) -> pd.DataFrame:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------------ helpers
    def resolve(self, overrides: dict | None = None) -> dict:
        p = dict(self.params)
        for k, v in (overrides or {}).items():
            if k not in p:
                raise ValueError(f"parámetro desconocido para {self.key}: {k}")
            p[k] = type(p[k])(v) if p[k] is not None else v
        return p

    def run(self, bars: pd.DataFrame, overrides: dict | None = None) -> pd.DataFrame:
        """Signals, validated and normalised (missing columns filled, NaN entries = 0)."""
        sig = self.signals(bars, self.resolve(overrides))
        if not isinstance(sig, pd.DataFrame) or not sig.index.equals(bars.index):
            raise ValueError(f"{self.key}: signals() must return a DataFrame on the bars' index")
        extra = set(sig.columns) - set(SIGNAL_COLUMNS)
        if extra:
            raise ValueError(f"{self.key}: unknown signal columns {sorted(extra)}")
        out = pd.DataFrame(index=bars.index)
        out["entry"] = pd.to_numeric(sig.get("entry", 0), errors="coerce").fillna(0).astype(int)
        if not out["entry"].isin([-1, 0, 1]).all():
            raise ValueError(f"{self.key}: entry must be -1, 0 or +1")
        if not self.allow_short and (out["entry"] < 0).any():
            raise ValueError(f"{self.key}: opens shorts but allow_short is False")
        flag = lambda c: (sig[c].fillna(False).astype(bool) if c in sig  # noqa: E731
                          else pd.Series(False, index=bars.index))
        out["exit_long"] = flag("exit") | flag("exit_long")
        out["exit_short"] = flag("exit") | flag("exit_short")
        for c in FLOAT_COLUMNS:
            out[c] = pd.to_numeric(sig[c], errors="coerce").astype(float) if c in sig else np.nan
        if (np.isfinite(out["entry_stop"]) & np.isfinite(out["entry_limit"])).any():
            raise ValueError(f"{self.key}: a bar cannot have both entry_stop and entry_limit")
        r = self.rank(bars, self.resolve(overrides))
        r = liquidity_rank(bars) if r is None else r
        if not isinstance(r, pd.Series) or not r.index.equals(bars.index):
            raise ValueError(f"{self.key}: rank() must return a Series on the bars' index")
        out["rank"] = pd.to_numeric(r, errors="coerce").astype(float)
        return out

    @classmethod
    def version(cls) -> str:
        """Changes whenever the strategy's code or default parameters change (results are then recomputed)."""
        try:
            src = inspect.getsource(cls)
        except (OSError, TypeError):
            src = cls.__qualname__
        return hashlib.sha256((src + repr(sorted(cls.params.items()))).encode()).hexdigest()[:16]

    @classmethod
    def info(cls) -> dict:
        return {"key": cls.key, "name": cls.name, "source": cls.source, "summary": cls.summary,
                "params": dict(cls.params), "param_grid": {k: list(v) for k, v in cls.param_grid.items()},
                "allow_short": cls.allow_short, "default_symbols": list(cls.default_symbols), "version": cls.version(),
                "universe": cls.universe, "max_positions": cls.positions(),
                "rank_rule": cls.rank_rule or DEFAULT_RANK_RULE, "rank_by_author": bool(cls.rank_rule),
                "day_trade": cls.day_trade, "overnight": cls.overnight, "paper_ready": not cls.overnight,
                "style": cls.style, "notes": cls.notes}

    @classmethod
    def positions(cls) -> int:
        return max(1, min(cls.max_positions or MAX_POSITIONS, MAX_POSITIONS))


REGISTRY: dict[str, Strategy] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if not getattr(cls, "key", None) or not getattr(cls, "name", None):
        raise ValueError(f"{cls.__name__}: key and name are required")
    if cls.key in REGISTRY and type(REGISTRY[cls.key]) is not cls:
        raise ValueError(f"duplicate strategy key {cls.key}")
    REGISTRY[cls.key] = cls()
    return cls


def load_all() -> dict[str, Strategy]:
    """Import every module in qsts.lab.strategies (each registers its strategies)."""
    from qsts.lab import strategies
    for mod in pkgutil.iter_modules(strategies.__path__):
        importlib.import_module(f"{strategies.__name__}.{mod.name}")
    return REGISTRY


def get(key: str) -> Strategy:
    load_all()
    if key not in REGISTRY:
        raise KeyError(key)
    return REGISTRY[key]
