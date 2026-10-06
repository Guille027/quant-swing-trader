"""Versioned feature registry.

A feature = (name, function, params, version). Its id is a content hash of those, so any change to
parameters or implementation version yields a new id and old experiments stay reproducible.

All features take a canonical OHLCV frame (plus optional benchmark close aligned on the same index)
and return a float Series. They must be causal; the test-suite checks every registered feature.
Bump `version` of a feature whenever its implementation changes.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from qsts.core.hashing import hash_obj
from qsts.indicators import core as ind
from qsts.indicators import structure as st

FeatureFn = Callable[..., pd.Series]


@dataclass(frozen=True)
class FeatureDef:
    name: str
    fn: FeatureFn
    category: str
    defaults: dict
    version: str = "1"
    needs_benchmark: bool = False


REGISTRY: dict[str, FeatureDef] = {}


def register(name: str, category: str, version: str = "1", needs_benchmark: bool = False, **defaults):
    def deco(fn: FeatureFn) -> FeatureFn:
        if name in REGISTRY:
            raise ValueError(f"duplicate feature {name}")
        REGISTRY[name] = FeatureDef(name, fn, category, defaults, version, needs_benchmark)
        return fn
    return deco


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    params: dict = field(default_factory=dict)

    def resolved(self) -> dict:
        d = REGISTRY[self.name]
        return {**d.defaults, **self.params}

    @property
    def key(self) -> str:
        p = self.resolved()
        return self.name + ("(" + ",".join(f"{k}={p[k]}" for k in sorted(p)) + ")" if p else "")

    @property
    def id(self) -> str:
        d = REGISTRY[self.name]
        return hash_obj({"name": self.name, "params": self.resolved(), "version": d.version})

    def to_dict(self) -> dict:
        return {"name": self.name, "params": self.resolved(), "version": REGISTRY[self.name].version}


# ------------------------------------------------------------------ optional computation cache
_LOCAL = threading.local()


class _FeatureCache:
    """LRU of computed feature Series keyed by (exact frame content, feature key). A hit returns exactly what
    the computation would return for that content, so results stay bit-identical. Thread-local and opt-in:
    only code inside `with feature_cache():` (e.g. the research loop) uses it."""

    def __init__(self, max_items: int):
        self.max_items, self.items = max_items, OrderedDict()
        self.hits = self.misses = 0
        self._last: tuple = (None, None)

    def token(self, df: pd.DataFrame) -> tuple:
        if self._last[0] is df:  # holding the reference guarantees the object was not recycled
            return self._last[1]
        cols = [c for c in df.columns if c != "available_at"]  # every input a feature may read (OHLCV, earnings...)
        h = pd.util.hash_pandas_object(df[cols], index=True).to_numpy()
        tok = (len(df), hashlib.blake2b(h.tobytes(), digest_size=16).hexdigest())
        self._last = (df, tok)
        return tok

    def get(self, key):
        v = self.items.get(key)
        if v is None:
            self.misses += 1
            return None
        self.hits += 1
        self.items.move_to_end(key)
        return v

    def put(self, key, value) -> None:
        self.items[key] = value
        if len(self.items) > self.max_items:
            self.items.popitem(last=False)


@contextmanager
def feature_cache(max_items: int = 6000):
    prev = getattr(_LOCAL, "cache", None)
    _LOCAL.cache = cache = _FeatureCache(max_items)
    try:
        yield cache
    finally:
        _LOCAL.cache = prev


class FeatureSet:
    def __init__(self, specs: list[FeatureSpec]):
        for s in specs:
            if s.name not in REGISTRY:
                raise KeyError(f"unknown feature {s.name}")
        self.specs = list(specs)

    @property
    def version(self) -> str:
        return hash_obj(sorted((s.to_dict() for s in self.specs), key=lambda d: (d["name"], str(d["params"]))))

    def compute(self, df: pd.DataFrame, benchmark_close: pd.Series | None = None) -> pd.DataFrame:
        cols = {}
        cache: _FeatureCache | None = getattr(_LOCAL, "cache", None)
        tok = cache.token(df) if cache is not None else None
        for s in self.specs:
            d = REGISTRY[s.name]
            kw = s.resolved()
            key = None
            if cache is not None:
                btok = None
                if d.needs_benchmark and benchmark_close is not None:
                    btok = hashlib.blake2b(pd.util.hash_pandas_object(benchmark_close.reindex(df.index)).to_numpy()
                                           .tobytes(), digest_size=16).hexdigest()
                key = (tok, s.id, btok)
                hit = cache.get(key)
                if hit is not None:
                    cols[s.key] = hit
                    continue
            if d.needs_benchmark:
                if benchmark_close is None:
                    raise ValueError(f"{s.name} requires a benchmark series")
                val = d.fn(df, benchmark_close.reindex(df.index), **kw)
            else:
                val = d.fn(df, **kw)
            cols[s.key] = val
            if key is not None:
                cache.put(key, val)
        return pd.DataFrame(cols, index=df.index).astype("float64")


def _b(x: pd.Series) -> pd.Series:
    return x.astype("float64")


# ------------------------------------------------------------------ trend
register("sma", "trend", n=20)(lambda df, n: ind.sma(df["close"], n))
register("ema", "trend", n=20)(lambda df, n: ind.ema(df["close"], n))
register("hma", "trend", n=20)(lambda df, n: ind.hma(df["close"], n))
register("dist_sma", "trend", n=50)(lambda df, n: df["close"] / ind.sma(df["close"], n) - 1)
register("dist_ema", "trend", n=20)(lambda df, n: df["close"] / ind.ema(df["close"], n) - 1)
register("sma_slope", "trend", n=50, lag=5)(lambda df, n, lag: ind.sma(df["close"], n).pct_change(lag))
register("adx", "trend", n=14)(lambda df, n: ind.adx(df, n)["adx"])
register("di_diff", "trend", n=14)(lambda df, n: (lambda a: a["plus_di"] - a["minus_di"])(ind.adx(df, n)))
register("supertrend_dir", "trend", n=10, mult=3.0)(lambda df, n, mult: ind.supertrend(df, n, mult)["direction"])
register("dist_vwap", "trend", n=20)(lambda df, n: df["close"] / ind.vwap_rolling(df, n) - 1)
register("ichimoku_tk", "trend")(lambda df: (lambda i: i["tenkan"] - i["kijun"])(ind.ichimoku(df)) / df["close"])

# ------------------------------------------------------------------ momentum
register("rsi", "momentum", n=14)(lambda df, n: ind.rsi(df["close"], n))
register("macd_hist", "momentum", fast=12, slow=26, signal=9)(
    lambda df, fast, slow, signal: ind.macd(df["close"], fast, slow, signal)["hist"] / df["close"])
register("stoch_k", "momentum", k=14, d=3)(lambda df, k, d: ind.stochastic(df, k, d)["k"])
register("roc", "momentum", n=10)(lambda df, n: ind.roc(df["close"], n))
register("cci", "momentum", n=20)(lambda df, n: ind.cci(df, n))
register("williams_r", "momentum", n=14)(lambda df, n: ind.williams_r(df, n))

# ------------------------------------------------------------------ volatility
register("atr", "volatility", n=14)(lambda df, n: ind.atr(df, n))
register("atr_pct", "volatility", n=14)(lambda df, n: ind.atr(df, n) / df["close"])
register("hist_vol", "volatility", n=20, ppy=252)(lambda df, n, ppy: ind.historical_volatility(df["close"], n, ppy))
register("bb_pct_b", "volatility", n=20, k=2.0)(lambda df, n, k: ind.bollinger(df["close"], n, k)["pct_b"])
register("bb_width", "volatility", n=20, k=2.0)(
    lambda df, n, k: (lambda b: (b["upper"] - b["lower"]) / b["mid"])(ind.bollinger(df["close"], n, k)))
register("keltner_pos", "volatility", n=20, mult=2.0)(
    lambda df, n, mult: (lambda kc: (df["close"] - kc["mid"]) / (kc["upper"] - kc["mid"]))(ind.keltner(df, n, mult)))
register("vol_ratio", "volatility", fast=10, slow=60)(
    lambda df, fast, slow: ind.historical_volatility(df["close"], fast) / ind.historical_volatility(df["close"], slow))

# ------------------------------------------------------------------ volume
register("rel_volume", "volume", n=20)(lambda df, n: ind.relative_volume(df, n))
register("volume_roc", "volume", n=10)(lambda df, n: ind.volume_roc(df, n))
register("obv_slope", "volume", n=20)(
    lambda df, n: ind.obv(df).diff(n) / df["volume"].rolling(n, min_periods=n).sum())

# ------------------------------------------------------------------ returns / risk
register("ret", "returns", n=1)(lambda df, n: df["close"].pct_change(n))
register("log_ret", "returns", n=1)(lambda df, n: np.log(df["close"]).diff(n))
register("drawdown", "returns", n=252)(
    lambda df, n: df["close"] / df["close"].rolling(n, min_periods=1).max() - 1)
register("gap", "returns")(lambda df: st.gap(df))
register("gap_abs_mean", "returns", n=20)(lambda df, n: st.gap(df).abs().rolling(n, min_periods=n).mean())

# ------------------------------------------------------------------ structure / price action
register("structure", "structure", k=3)(lambda df, k: _b(st.swing_points(df, k)["structure"]))
register("dist_resistance", "structure", k=3)(lambda df, k: df["close"] / st.swing_points(df, k)["resistance"] - 1)
register("dist_support", "structure", k=3)(lambda df, k: df["close"] / st.swing_points(df, k)["support"] - 1)
register("breakout_up", "structure", n=20)(lambda df, n: _b(st.breakout(df, n)["breakout_up"]))
register("breakout_down", "structure", n=20)(lambda df, n: _b(st.breakout(df, n)["breakout_down"]))
register("pullback", "structure", trend_n=50, fast_n=10)(lambda df, trend_n, fast_n: _b(st.pullback(df, trend_n, fast_n)))
register("inside_bar", "price_action")(lambda df: _b(st.inside_bar(df)))
register("engulfing", "price_action")(lambda df: _b(st.engulfing(df)))


# ------------------------------------------------------------------ candle shape / patterns (single + multi-bar)
def _range(df):
    return (df["high"] - df["low"]).replace(0, np.nan)


register("clv", "price_action")(  # where the close sits in the day's range: 0 = at the low, 1 = at the high
    lambda df: ((df["close"] - df["low"]) / _range(df)).fillna(0.5))
register("body_pct", "price_action")(  # signed candle body as a share of the range (-1..1)
    lambda df: ((df["close"] - df["open"]) / _range(df)).fillna(0.0))
register("upper_wick", "price_action")(
    lambda df: ((df["high"] - df[["open", "close"]].max(axis=1)) / _range(df)).fillna(0.0))
register("lower_wick", "price_action")(
    lambda df: ((df[["open", "close"]].min(axis=1) - df["low"]) / _range(df)).fillna(0.0))
register("range_rank", "price_action", n=7)(  # 0 = narrowest range of the last n days (NR7-type squeeze)
    lambda df, n: (df["high"] - df["low"]).rolling(n, min_periods=n).rank(pct=True))
register("range_ratio", "price_action", n=20)(  # today's range vs its n-day average (expansion > 1)
    lambda df, n: (df["high"] - df["low"]) / (df["high"] - df["low"]).rolling(n, min_periods=n).mean())


@register("streak", "price_action")
def _streak(df):
    """Consecutive up closes (+k) or down closes (-k) up to today."""
    d = np.sign(df["close"].diff()).fillna(0.0)
    grp = (d != d.shift()).cumsum()
    return d * d.groupby(grp).cumcount().add(1).where(d != 0, 0)


# ------------------------------------------------------------------ calendar (known in advance: no look-ahead)
register("day_of_week", "calendar")(lambda df: pd.Series(df.index.dayofweek, index=df.index, dtype="float64"))


@register("month_day", "calendar")
def _month_day(df):
    """Trading session number within the month (1 = first session)."""
    key = df.index.year * 100 + df.index.month
    return pd.Series(pd.Series(1, index=df.index).groupby(key).cumsum().to_numpy(dtype="float64"), index=df.index)


@register("month_end_in", "calendar")
def _month_end_in(df):
    """Sessions left until the month's last session (0 = last session), from the NYSE calendar."""
    from qsts.data.bars import nyse_schedule
    if len(df) == 0:
        return pd.Series(dtype="float64")
    sess = nyse_schedule(df.index.min() - pd.Timedelta(days=1), df.index.max() + pd.Timedelta(days=40)).index
    key = sess.year * 100 + sess.month
    left = pd.Series(1, index=sess).groupby(key).cumcount(ascending=False)
    day = df.index.tz_convert("UTC").normalize() if df.index.tz is not None else df.index.normalize()
    return pd.Series(left.reindex(day).to_numpy(dtype="float64"), index=df.index)


# ------------------------------------------------------------------ events (quarterly results)
# Read the point-in-time columns built by qsts.data.earnings (attached by AppContext.research_frame);
# NaN when the symbol has no earnings data. Causality is guaranteed by the column builder (tested there).
def _col(df, name):
    return df[name].astype("float64") if name in df else pd.Series(np.nan, index=df.index)


register("days_since_earnings", "events")(lambda df: _col(df, "earn_days_since"))
register("earnings_surprise", "events")(lambda df: _col(df, "earn_surprise"))
register("days_to_earnings", "events")(lambda df: _col(df, "earn_days_to"))


# ------------------------------------------------------------------ cross-asset (benchmark)
@register("rel_strength", "relative", needs_benchmark=True, n=63)
def _rel_strength(df, bench, n):
    return df["close"].pct_change(n) - bench.pct_change(n)


@register("beta", "relative", needs_benchmark=True, n=126)
def _beta(df, bench, n):
    r, b = df["close"].pct_change(), bench.pct_change()
    return r.rolling(n, min_periods=n).cov(b) / b.rolling(n, min_periods=n).var()


@register("corr_bench", "relative", needs_benchmark=True, n=63)
def _corr(df, bench, n):
    return df["close"].pct_change().rolling(n, min_periods=n).corr(bench.pct_change())
