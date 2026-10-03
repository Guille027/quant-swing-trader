"""Backward split / dividend adjustment of RAW bars (DECISIONS #5).

Stored prices are raw. Research uses backward-adjusted prices (the last bar keeps its raw value):
- split with ratio r (new shares per old share, 4.0 for a 4:1 split) on ex-date E:
  prices of bars before E are multiplied by 1/r and volumes by r;
- cash dividend D (per share, raw, i.e. in the share basis of its own date) on ex-date E:
  prices of bars before E are multiplied by (1 - D / C), C = raw close of the last bar before E.
This is the standard CRSP/Yahoo "Adj Close" construction. `raw_close` keeps the unadjusted close.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MODES = ("none", "split", "total")


def _ex(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def adjustment_factors(df: pd.DataFrame, actions: pd.DataFrame | None, mode: str = "total") -> tuple[np.ndarray, np.ndarray]:
    """(price_factor, split_factor) per bar; multiply raw prices by price_factor, divide volume by split_factor."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    n = len(df)
    price_f, split_f = np.ones(n), np.ones(n)
    if mode == "none" or actions is None or len(actions) == 0:
        return price_f, split_f
    idx = df.index
    raw_close = df["raw_close"].values if "raw_close" in df else df["close"].values
    for a in actions.sort_values("ex_date", kind="stable").itertuples(index=False):
        ex = _ex(a.ex_date)
        before = idx < ex
        if not before.any():
            continue
        if a.kind == "split":
            if not a.value > 0:
                raise ValueError(f"invalid split ratio {a.value} on {ex.date()}")
            price_f[before] /= a.value
            split_f[before] *= a.value
        elif a.kind == "dividend":
            if mode != "total":
                continue
            prev = raw_close[np.flatnonzero(before)[-1]]
            f = 1.0 - a.value / prev
            if not 0 < f < 1:
                raise ValueError(f"dividend {a.value} on {ex.date()} inconsistent with prior close {prev}")
            price_f[before] *= f
        else:
            raise ValueError(f"unknown corporate action kind {a.kind!r}")
    return price_f, split_f


def adjust(df: pd.DataFrame, actions: pd.DataFrame | None, mode: str = "total") -> pd.DataFrame:
    """Adjusted copy of raw bars; adds `raw_close` (unadjusted) and `adj_factor` (price multiplier)."""
    out = df.copy()
    if "raw_close" not in out:
        out["raw_close"] = out["close"]
    pf, sf = adjustment_factors(out, actions, mode)
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].values * pf
    if "volume" in out:
        out["volume"] = out["volume"].values * sf
    out["adj_factor"] = pf
    return out
