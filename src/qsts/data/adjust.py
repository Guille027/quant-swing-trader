"""Corporate-action adjustment (backward adjustment).

mode="split": divide pre-split prices by the cumulative split ratio, multiply volume.
mode="total": additionally apply dividend factors (1 - div / prev_close) -> total-return series.

Backward adjustment rescales past *levels* using later events. Returns/indicators computed
on it are unaffected in a look-ahead sense (ratios are preserved), but absolute price levels
are not what was quoted at the time. Therefore the system keeps `raw_close` alongside, and any
rule that depends on absolute price (e.g. min price filters, share-count sizing) must use raw.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PRICE_COLS = ["open", "high", "low", "close"]


def adjustment_factors(df: pd.DataFrame, actions: pd.DataFrame, mode: str = "split") -> pd.Series:
    if mode not in ("split", "total"):
        raise ValueError(mode)
    idx = df.index
    factor = pd.Series(1.0, index=idx)
    if actions is None or actions.empty:
        return factor
    for _, a in actions.sort_values("ex_date").iterrows():
        ex = pd.Timestamp(a["ex_date"])
        before = idx < ex
        if not before.any():
            continue
        if a["kind"] == "split":
            if a["value"] <= 0:
                raise ValueError("split ratio must be positive")
            factor[before] /= float(a["value"])
        elif a["kind"] == "dividend" and mode == "total":
            prev_close = df["close"][before].iloc[-1]
            f = 1.0 - float(a["value"]) / prev_close
            if not 0 < f <= 1:
                raise ValueError(f"invalid dividend factor at {ex}: {f}")
            factor[before] *= f
    return factor


def adjust(df: pd.DataFrame, actions: pd.DataFrame, mode: str = "split") -> pd.DataFrame:
    f = adjustment_factors(df, actions, mode)
    out = df.copy()
    out["raw_close"] = df["close"]
    for c in PRICE_COLS:
        out[c] = df[c] * f
    split_only = adjustment_factors(df, actions[actions["kind"] == "split"] if actions is not None and not actions.empty else actions, "split")
    out["volume"] = df["volume"] / split_only.replace(0, np.nan)
    out["adj_factor"] = f
    return out
