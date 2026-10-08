"""Indicators in TradingView (Pine Script) terms, for writing strategies. All causal: the value at a bar uses only
that bar and earlier ones. Moving averages, RSI, ATR, MACD, Bollinger, Supertrend... come from qsts.indicators
(already tested for causality); this module adds Pine's helpers with the same names and meaning."""
from __future__ import annotations

import numpy as np
import pandas as pd

from qsts.indicators.core import (ichimoku, adx, atr, bollinger, cci, ema, hma, keltner, macd, obv, roc, rsi, sma,  # noqa: F401
                                  stochastic, supertrend, true_range, williams_r, wma)


def crossover(a, b) -> pd.Series:
    """ta.crossover: `a` goes from <= b to > b on this bar."""
    a, b = _s(a), _s(b, a.index)
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossunder(a, b) -> pd.Series:
    """ta.crossunder: `a` goes from >= b to < b on this bar."""
    a, b = _s(a), _s(b, a.index)
    return (a < b) & (a.shift(1) >= b.shift(1))


def highest(s: pd.Series, n: int) -> pd.Series:
    """ta.highest: max of the last n bars INCLUDING this one (use .shift(1) for 'previous n bars')."""
    return s.rolling(n, min_periods=n).max()


def lowest(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).min()


def rma(s: pd.Series, n: int) -> pd.Series:
    """ta.rma (Wilder's moving average, seeded with the SMA of the first n values)."""
    from qsts.indicators.core import _wilder
    return _wilder(s, n)


def stdev(s: pd.Series, n: int) -> pd.Series:
    """ta.stdev (population standard deviation, as in Pine)."""
    return s.rolling(n, min_periods=n).std(ddof=0)


def change(s: pd.Series, n: int = 1) -> pd.Series:
    return s - s.shift(n)


def barssince(cond: pd.Series) -> pd.Series:
    """ta.barssince: bars since `cond` was last true (NaN before the first time)."""
    c = cond.fillna(False).astype(bool).to_numpy()
    out = np.full(len(c), np.nan)
    last = -1
    for i, v in enumerate(c):
        if v:
            last = i
        if last >= 0:
            out[i] = i - last
    return pd.Series(out, index=cond.index)


def valuewhen(cond: pd.Series, src: pd.Series, occurrence: int = 0) -> pd.Series:
    """ta.valuewhen: value of `src` the last (occurrence+1)-th time `cond` was true."""
    vals = src.where(cond.fillna(False).astype(bool))
    if occurrence == 0:
        return vals.ffill()
    idx = np.flatnonzero(cond.fillna(False).to_numpy())
    out = np.full(len(src), np.nan)
    v = src.to_numpy(dtype=float)
    for k in range(occurrence, len(idx)):
        end = idx[k + 1] if k + 1 < len(idx) else len(src)
        out[idx[k]:end] = v[idx[k - occurrence]]
    return pd.Series(out, index=src.index)


def donchian(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Highest high / lowest low of the PREVIOUS n bars (a breakout above `upper` is a new n-bar high)."""
    return pd.DataFrame({"upper": df["high"].rolling(n, min_periods=n).max().shift(1),
                         "lower": df["low"].rolling(n, min_periods=n).min().shift(1)})


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Heikin-Ashi candles (each one uses only its own bar and the previous HA candle)."""
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))  # noqa: E741
    ha_c = (o + h + l + c) / 4
    ha_o = np.empty(len(o))
    ha_o[0] = (o[0] + c[0]) / 2
    for i in range(1, len(o)):
        ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2
    return pd.DataFrame({"open": ha_o, "high": np.maximum.reduce([h, ha_o, ha_c]),
                         "low": np.minimum.reduce([l, ha_o, ha_c]), "close": ha_c}, index=df.index)


def ibs(df: pd.DataFrame) -> pd.Series:
    """Internal bar strength: where the close sits in the day's range (0 = at the low, 1 = at the high)."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return (df["close"] - df["low"]) / rng


def psar(df: pd.DataFrame, start: float = 0.02, inc: float = 0.02, maximum: float = 0.2) -> pd.DataFrame:
    """ta.sar: Welles Wilder's Parabolic SAR (1978). `direction` +1 while the SAR is below the price (uptrend)."""
    h, l = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)  # noqa: E741
    n = len(h)
    sar, d = np.full(n, np.nan), np.zeros(n)
    if n < 2:
        return pd.DataFrame({"sar": sar, "direction": d}, index=df.index)
    up = h[1] >= h[0]
    ep = h[1] if up else l[1]
    s = l[0] if up else h[0]
    af = start
    sar[1], d[1] = s, 1 if up else -1
    for i in range(2, n):
        s = s + af * (ep - s)
        if up:
            s = min(s, l[i - 1], l[i - 2])
            if l[i] < s:  # reversal
                up, s, ep, af = False, ep, l[i], start
            elif h[i] > ep:
                ep, af = h[i], min(af + inc, maximum)
        else:
            s = max(s, h[i - 1], h[i - 2])
            if h[i] > s:
                up, s, ep, af = True, ep, h[i], start
            elif l[i] < ep:
                ep, af = l[i], min(af + inc, maximum)
        sar[i], d[i] = s, 1 if up else -1
    d[np.isnan(sar)] = np.nan
    return pd.DataFrame({"sar": sar, "direction": d}, index=df.index)


def month_end(index: pd.DatetimeIndex) -> pd.Series:
    """True on the last NYSE session of each month. Uses the exchange calendar (known in advance), never prices,
    so it is causal: on the last session of a month you already know tomorrow is in a new month."""
    from qsts.data.bars import nyse_sessions
    if not len(index):
        return pd.Series(False, index=index)
    cal = nyse_sessions(index[0], index[-1] + pd.Timedelta(days=45))
    nxt = pd.Series(cal[1:], index=cal[:-1])
    nx = nxt.reindex(index)
    return pd.Series((nx.dt.month != index.month).to_numpy() & nx.notna().to_numpy(), index=index)


def _s(x, index=None) -> pd.Series:
    if isinstance(x, pd.Series):
        return x
    return pd.Series(float(x), index=index)
