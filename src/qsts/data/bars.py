"""Canonical bar representation and timeframe handling.

Canonical OHLCV frame:
- index: tz-aware UTC DatetimeIndex named "ts" = bar OPEN time
  (daily/weekly bars: the session date at 00:00 UTC of the first session in the bar)
- columns: open, high, low, close, volume, available_at
- `available_at` (UTC) = the earliest moment the COMPLETED bar is known.
  Intraday: open + duration (clipped to session close). Daily: that session's close.
  Weekly: close of the last session in the week.

Every look-ahead protection in the system is expressed in terms of `available_at`:
a decision taken at time T may only use bars with available_at <= T.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

import pandas as pd
import pandas_market_calendars as mcal

OHLCV = ["open", "high", "low", "close", "volume"]


class Timeframe(str, Enum):
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"

    @property
    def is_intraday(self) -> bool:
        return self in (Timeframe.H1, Timeframe.H4)

    @property
    def rank(self) -> int:
        return [Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1].index(self)


@lru_cache(maxsize=32)
def _schedule(start: str, end: str) -> pd.DataFrame:
    cal = mcal.get_calendar("NYSE")
    return cal.schedule(start_date=start, end_date=end)


def nyse_schedule(start, end) -> pd.DataFrame:
    """NYSE sessions with UTC market_open / market_close (handles holidays & early closes)."""
    s = pd.Timestamp(start).strftime("%Y-%m-%d")
    e = pd.Timestamp(end).strftime("%Y-%m-%d")
    return _schedule(s, e)


def nyse_sessions(start, end) -> pd.DatetimeIndex:
    sched = nyse_schedule(start, end)
    return pd.DatetimeIndex(sched.index).tz_localize("UTC") if sched.index.tz is None else sched.index


def _session_close_map(dates: pd.DatetimeIndex) -> pd.Series:
    if len(dates) == 0:
        return pd.Series(dtype="datetime64[ns, UTC]")
    sched = nyse_schedule(dates.min() - pd.Timedelta(days=7), dates.max() + pd.Timedelta(days=7))
    closes = sched["market_close"]
    closes.index = pd.DatetimeIndex(closes.index).tz_localize("UTC").normalize()
    return closes


def compute_available_at(index: pd.DatetimeIndex, timeframe: Timeframe) -> pd.DatetimeIndex:
    if index.tz is None:
        raise ValueError("bar index must be tz-aware UTC")
    if timeframe is Timeframe.D1:
        closes = _session_close_map(index)
        days = index.normalize()
        out = closes.reindex(days)
        # Bars on non-sessions are invalid data; they get NaT and are rejected by quality checks.
        return pd.DatetimeIndex(out.values, tz="UTC")
    if timeframe is Timeframe.W1:
        closes = _session_close_map(index)
        out = []
        for ts in index:
            week_end = ts.normalize() + pd.Timedelta(days=6)
            c = closes[(closes.index >= ts.normalize()) & (closes.index <= week_end)]
            out.append(c.iloc[-1] if len(c) else pd.NaT)
        return pd.DatetimeIndex(out, tz="UTC") if len(out) else pd.DatetimeIndex([], tz="UTC")
    dur = pd.Timedelta(hours=1 if timeframe is Timeframe.H1 else 4)
    closes = _session_close_map(index)
    day_close = closes.reindex(index.normalize())
    naive_end = pd.Series(index + dur, index=index)
    dc = pd.Series(day_close.to_numpy(), index=index)
    end = pd.DatetimeIndex(naive_end.where(dc.isna() | (naive_end <= dc), dc))
    return end


def to_canonical(df: pd.DataFrame, timeframe: Timeframe) -> pd.DataFrame:
    """Normalize column names / index / dtypes and attach `available_at`."""
    out = df.copy()
    out.columns = [str(c).lower().replace(" ", "_") for c in out.columns]
    missing = [c for c in OHLCV if c not in out.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    idx = pd.DatetimeIndex(out.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    if not timeframe.is_intraday:
        idx = idx.normalize()
    out.index = idx
    out.index.name = "ts"
    out = out[OHLCV + [c for c in out.columns if c not in OHLCV and c != "available_at"]]
    for c in OHLCV:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    out["available_at"] = compute_available_at(out.index, timeframe)
    return out


def resample_intraday_to_4h(h1: pd.DataFrame) -> pd.DataFrame:
    """Build session-aligned 4H bars from canonical 1H bars.

    Each regular session is split into blocks of 4 hourly bars starting at the open
    (09:30-13:30, 13:30-16:00 ET). A 4H bar is only emitted from completed hourly bars, and its
    available_at is the available_at of its last constituent hour (never earlier).
    """
    if h1.empty:
        return h1.copy()
    df = h1.sort_index().copy()
    df["_ts"] = df.index
    day = df.index.normalize()
    pos = df.groupby(day).cumcount()
    block = (pos // 4).to_numpy()
    key = [day, block]
    g = df.groupby(key)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "available_at": g["available_at"].max(),
    })
    out.index = pd.DatetimeIndex(g["_ts"].first().array)
    out.index.name = "ts"
    return out


def resample_daily_to_weekly(d1: pd.DataFrame) -> pd.DataFrame:
    if d1.empty:
        return d1.copy()
    df = d1.sort_index().copy()
    df["_ts"] = df.index
    week = df.index.tz_localize(None).to_period("W-SUN")
    g = df.groupby(week)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "available_at": g["available_at"].max(),
    })
    out.index = pd.DatetimeIndex(g["_ts"].first().array)
    out.index.name = "ts"
    return out


def align_higher_timeframe(
    decision_times: pd.DatetimeIndex, higher: pd.DataFrame, columns: list[str] | None = None,
    suffix: str = "",
) -> pd.DataFrame:
    """For each decision time T, return the latest higher-timeframe row with available_at <= T.

    This is the ONLY sanctioned way to combine timeframes: it makes it impossible for a
    lower-timeframe decision to see a higher-timeframe bar that had not closed yet.
    """
    cols = columns or [c for c in higher.columns if c != "available_at"]
    right = higher[cols + ["available_at"]].sort_values("available_at").reset_index(drop=True)
    right = right.dropna(subset=["available_at"])
    left = pd.DataFrame({"decision_time": pd.DatetimeIndex(decision_times)})
    left["_order"] = range(len(left))
    left = left.sort_values("decision_time")
    merged = pd.merge_asof(left, right, left_on="decision_time", right_on="available_at",
                           direction="backward", allow_exact_matches=True)
    merged = merged.sort_values("_order")
    merged.index = pd.DatetimeIndex(decision_times)
    out = merged[cols]
    if suffix:
        out = out.add_suffix(suffix)
    return out
