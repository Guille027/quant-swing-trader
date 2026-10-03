"""Market Regime Engine (rule-based, causal).

Labels:
  trend:      BULL | BEAR | SIDEWAYS | TRANSITION
  volatility: HIGH_VOLATILITY | NORMAL_VOLATILITY | LOW_VOLATILITY

Volatility is classified by the percentile of current realised vol within its own TRAILING history
(rolling window), never the full-sample distribution -- a full-sample percentile would leak the
future. Whether any regime label has predictive value is an empirical question for the research
engine; this module only labels.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qsts.indicators.core import historical_volatility, sma


@dataclass(frozen=True)
class RegimeConfig:
    trend_ma: int = 200  # long-term trend reference (common industry convention; testable)
    slope_lag: int = 20  # bars over which MA slope is measured
    band: float = 0.02  # |close/MA - 1| below this is "near the average" -> SIDEWAYS candidate
    vol_window: int = 20
    vol_lookback: int = 252  # trailing window for vol percentile
    high_vol_pct: float = 0.8
    low_vol_pct: float = 0.2
    transition_bars: int = 5  # label TRANSITION for this many bars after a trend flip
    vix_high: float | None = None  # optional absolute VIX override (e.g. 30); None = unused


def _rolling_pct_rank(s: pd.Series, n: int) -> pd.Series:
    def rank_last(x):
        return (x[:-1] < x[-1]).mean() if len(x) > 1 else np.nan
    return s.rolling(n, min_periods=max(20, n // 4)).apply(rank_last, raw=True)


def classify(bench: pd.DataFrame, cfg: RegimeConfig = RegimeConfig(), vix: pd.Series | None = None) -> pd.DataFrame:
    c = bench["close"]
    ma = sma(c, cfg.trend_ma)
    dist = c / ma - 1
    slope = ma / ma.shift(cfg.slope_lag) - 1
    raw = np.select(
        [(dist > cfg.band) & (slope > 0), (dist < -cfg.band) & (slope < 0)],
        ["BULL", "BEAR"], "SIDEWAYS")
    raw = pd.Series(raw, index=c.index).where(ma.notna() & slope.notna())
    trend = raw.copy()
    last_flip = None
    prev = None
    for i, (ts, lab) in enumerate(raw.items()):
        if pd.isna(lab):
            continue
        if prev is not None and lab != prev:
            last_flip = i
        prev = lab
        if last_flip is not None and i - last_flip < cfg.transition_bars:
            trend.iloc[i] = "TRANSITION"
    vol = historical_volatility(c, cfg.vol_window)
    pct = _rolling_pct_rank(vol, cfg.vol_lookback)
    vreg = pd.Series(np.select([pct >= cfg.high_vol_pct, pct <= cfg.low_vol_pct],
                               ["HIGH_VOLATILITY", "LOW_VOLATILITY"], "NORMAL_VOLATILITY"), index=c.index)
    if vix is not None and cfg.vix_high is not None:
        vreg[vix.reindex(c.index) >= cfg.vix_high] = "HIGH_VOLATILITY"
    vreg = vreg.where(pct.notna())
    return pd.DataFrame({"trend": trend, "volatility": vreg, "dist_ma": dist, "ma_slope": slope, "vol_pct": pct})
