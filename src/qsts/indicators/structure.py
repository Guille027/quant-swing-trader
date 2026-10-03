"""Market structure and price-action primitives.

Swing points need confirmation from k bars on the right. A naive implementation marks the pivot at
bar t-k using bars up to t -- which is look-ahead if read at t-k. Here every output is indexed at the
CONFIRMATION bar: on bar t we only know swings whose confirmation window has fully elapsed.

No pattern here is assumed to be predictive; each is just a measurable condition to be tested.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def swing_points(df: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    w = 2 * k + 1
    hi, lo = df["high"], df["low"]
    # pivot candidate is the bar k bars ago; confirmed at the current bar
    is_sh = hi.shift(k) == hi.rolling(w, min_periods=w).max()
    is_sl = lo.shift(k) == lo.rolling(w, min_periods=w).min()
    sh_price = hi.shift(k).where(is_sh)
    sl_price = lo.shift(k).where(is_sl)
    last_sh = sh_price.ffill()
    last_sl = sl_price.ffill()
    prev_sh = sh_price.dropna().shift(1).reindex(df.index).ffill()
    prev_sl = sl_price.dropna().shift(1).reindex(df.index).ffill()
    out = pd.DataFrame({
        "swing_high_confirmed": is_sh.fillna(False),
        "swing_low_confirmed": is_sl.fillna(False),
        "resistance": last_sh,  # most recent confirmed swing high
        "support": last_sl,  # most recent confirmed swing low
        "higher_high": last_sh > prev_sh,
        "higher_low": last_sl > prev_sl,
        "lower_high": last_sh < prev_sh,
        "lower_low": last_sl < prev_sl,
    }, index=df.index)
    up = out["higher_high"] & out["higher_low"]
    dn = out["lower_high"] & out["lower_low"]
    out["structure"] = np.select([up, dn], [1, -1], 0)
    return out


def breakout(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Close beyond the prior n-bar range (prior = excluding current bar)."""
    hh = df["high"].shift(1).rolling(n, min_periods=n).max()
    ll = df["low"].shift(1).rolling(n, min_periods=n).min()
    return pd.DataFrame({"breakout_up": df["close"] > hh, "breakout_down": df["close"] < ll,
                         "range_high": hh, "range_low": ll})


def pullback(df: pd.DataFrame, trend_n: int = 50, fast_n: int = 10) -> pd.Series:
    """+1: uptrend (close>SMA trend_n) with close below fast SMA; -1 mirror; else 0."""
    t = df["close"].rolling(trend_n, min_periods=trend_n).mean()
    f = df["close"].rolling(fast_n, min_periods=fast_n).mean()
    c = df["close"]
    return pd.Series(np.select([(c > t) & (c < f), (c < t) & (c > f)], [1, -1], 0), index=df.index)


# ---------------------------------------------------------------- price action
def inside_bar(df: pd.DataFrame) -> pd.Series:
    return (df["high"] < df["high"].shift(1)) & (df["low"] > df["low"].shift(1))


def engulfing(df: pd.DataFrame) -> pd.Series:
    o, c, po, pc = df["open"], df["close"], df["open"].shift(1), df["close"].shift(1)
    bull = (c > o) & (pc < po) & (c >= po) & (o <= pc)
    bear = (c < o) & (pc > po) & (c <= po) & (o >= pc)
    return pd.Series(np.select([bull, bear], [1, -1], 0), index=df.index)


def gap(df: pd.DataFrame) -> pd.Series:
    """Opening gap vs previous close, as a fraction."""
    return df["open"] / df["close"].shift(1) - 1
