"""Technical indicators. All are strictly causal: value at bar t uses only bars <= t.

Every function takes a canonical OHLCV DataFrame (or Series) and returns a Series/DataFrame
aligned to the input index. Causality is verified generically in tests by checking that
computing on a truncated history reproduces the prefix of the full computation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- helpers
def _wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder smoothing: seeded with the SMA of the first n valid values, then
    x_t = x_{t-1} + (v_t - x_{t-1}) / n. NaNs before the first valid value are skipped."""
    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    valid = np.flatnonzero(~np.isnan(v))
    if len(valid) < n:
        return pd.Series(out, index=s.index)
    start = valid[0]
    seed_end = start + n
    if np.isnan(v[start:seed_end]).any():
        # non-contiguous leading data: fall back to first contiguous run
        return pd.Series(out, index=s.index)
    acc = v[start:seed_end].mean()
    out[seed_end - 1] = acc
    for i in range(seed_end, len(v)):
        if not np.isnan(v[i]):
            acc += (v[i] - acc) / n
        out[i] = acc
    return pd.Series(out, index=s.index)


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


# ---------------------------------------------------------------- trend
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n, min_periods=n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def hma(s: pd.Series, n: int) -> pd.Series:
    return wma(2 * wma(s, max(n // 2, 1)) - wma(s, n), max(int(np.sqrt(n)), 1))


def vwap_rolling(df: pd.DataFrame, n: int) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).rolling(n, min_periods=n).sum() / df["volume"].rolling(n, min_periods=n).sum()


def vwap_session(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP that resets each session (UTC date == NYSE session date for RTH bars)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.normalize()
    return (tp * df["volume"]).groupby(day).cumsum() / df["volume"].groupby(day).cumsum()


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr_ = _wilder(true_range(df), n)
    pdi = 100 * _wilder(plus_dm, n) / atr_
    mdi = 100 * _wilder(minus_dm, n) / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"adx": _wilder(dx, n), "plus_di": pdi, "minus_di": mdi})


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.DataFrame:
    a = atr(df, n)
    hl2 = (df["high"] + df["low"]) / 2
    ub, lb = (hl2 + mult * a).to_numpy(), (hl2 - mult * a).to_numpy()
    c = df["close"].to_numpy()
    fub, flb = ub.copy(), lb.copy()
    st = np.full(len(df), np.nan)
    direction = np.zeros(len(df))
    for i in range(1, len(df)):
        if np.isnan(ub[i]):
            continue
        if not np.isnan(fub[i - 1]):
            fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
            flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        prev = direction[i - 1] if direction[i - 1] != 0 else 1
        if prev == 1:
            direction[i] = -1 if c[i] < flb[i] else 1
        else:
            direction[i] = 1 if c[i] > fub[i] else -1
        st[i] = flb[i] if direction[i] == 1 else fub[i]
    direction[np.isnan(st)] = np.nan
    return pd.DataFrame({"supertrend": st, "direction": direction}, index=df.index)


def ichimoku(df: pd.DataFrame, tenkan: int = 9, kijun: int = 26, senkou_b: int = 52) -> pd.DataFrame:
    """Ichimoku WITHOUT the conventional forward/backward plotting shifts.

    The classic chikou span is the close plotted 26 bars back -- using it at bar t as if it were
    known then is look-ahead. Senkou spans are reported as the value *computed* at t (which would be
    plotted at t+kijun). Strategies needing "the cloud under today's price" must use
    senkou_*.shift(kijun), which is causal.
    """
    mid = lambda n: (df["high"].rolling(n, min_periods=n).max() + df["low"].rolling(n, min_periods=n).min()) / 2  # noqa: E731
    t, k = mid(tenkan), mid(kijun)
    return pd.DataFrame({"tenkan": t, "kijun": k, "senkou_a": (t + k) / 2, "senkou_b": mid(senkou_b)})


# ---------------------------------------------------------------- momentum
def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    gain = _wilder(d.clip(lower=0), n)
    loss = _wilder((-d).clip(lower=0), n)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(s, fast) - ema(s, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def stochastic(df: pd.DataFrame, k: int = 14, d: int = 3) -> pd.DataFrame:
    lo = df["low"].rolling(k, min_periods=k).min()
    hi = df["high"].rolling(k, min_periods=k).max()
    pk = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    return pd.DataFrame({"k": pk, "d": pk.rolling(d, min_periods=d).mean()})


def roc(s: pd.Series, n: int = 10) -> pd.Series:
    return 100 * (s / s.shift(n) - 1)


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    m = tp.rolling(n, min_periods=n).mean()
    md = tp.rolling(n, min_periods=n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - m) / (0.015 * md)


def williams_r(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hi = df["high"].rolling(n, min_periods=n).max()
    lo = df["low"].rolling(n, min_periods=n).min()
    return -100 * (hi - df["close"]) / (hi - lo).replace(0, np.nan)


# ---------------------------------------------------------------- volatility
def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _wilder(true_range(df), n)


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    m = sma(s, n)
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame({"mid": m, "upper": m + k * sd, "lower": m - k * sd,
                         "pct_b": (s - (m - k * sd)) / (2 * k * sd).replace(0, np.nan)})


def keltner(df: pd.DataFrame, n: int = 20, mult: float = 2.0, atr_n: int = 10) -> pd.DataFrame:
    m = ema(df["close"], n)
    a = atr(df, atr_n)
    return pd.DataFrame({"mid": m, "upper": m + mult * a, "lower": m - mult * a})


def historical_volatility(s: pd.Series, n: int = 20, periods_per_year: int = 252) -> pd.Series:
    return np.log(s).diff().rolling(n, min_periods=n).std() * np.sqrt(periods_per_year)


# ---------------------------------------------------------------- volume
def obv(df: pd.DataFrame) -> pd.Series:
    sign = np.sign(df["close"].diff()).fillna(0)
    return (sign * df["volume"]).cumsum()


def relative_volume(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Volume vs average of the PREVIOUS n bars (excludes current bar from the baseline)."""
    return df["volume"] / df["volume"].shift(1).rolling(n, min_periods=n).mean()


def volume_roc(df: pd.DataFrame, n: int = 10) -> pd.Series:
    return roc(df["volume"], n)
