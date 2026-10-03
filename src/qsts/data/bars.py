"""Bars, timeframes and the NYSE calendar.

Conventions (see docs/DECISIONS.md #3-4):
- Bar index `ts` = bar OPEN time, tz-aware UTC. Daily/weekly bars are dated by session at 00:00 UTC.
- `available_at` = the moment the bar is complete (session close for daily bars, early closes honoured;
  min(open + duration, session close) for intraday bars). A decision at time T may only use bars with
  available_at <= T. Cross-timeframe joins go through `align_higher_timeframe` only.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]
_CAL_START, _CAL_END = "1990-01-01", "2035-12-31"


class Timeframe(str, Enum):
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"

    @property
    def intraday(self) -> bool:
        return self in (Timeframe.H1, Timeframe.H4)

    @property
    def duration(self) -> pd.Timedelta:
        return {Timeframe.H1: pd.Timedelta(hours=1), Timeframe.H4: pd.Timedelta(hours=4),
                Timeframe.D1: pd.Timedelta(days=1), Timeframe.W1: pd.Timedelta(weeks=1)}[self]


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _compute_schedule(start, end) -> pd.DataFrame:
    import pandas_market_calendars as mcal
    s = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)
    out = pd.DataFrame({"market_open": pd.DatetimeIndex(s["market_open"]).tz_convert("UTC").as_unit("ns"),
                        "market_close": pd.DatetimeIndex(s["market_close"]).tz_convert("UTC").as_unit("ns")},
                       index=pd.DatetimeIndex(s.index).tz_localize("UTC").as_unit("ns"))
    out.index.name = "session"
    return out


@lru_cache(maxsize=1)
def _full_schedule() -> pd.DataFrame:
    return _compute_schedule(_CAL_START, _CAL_END)


def nyse_schedule(start, end) -> pd.DataFrame:
    """NYSE sessions in [start, end]: index = session date (00:00 UTC), columns market_open/market_close (UTC)."""
    s, e = _utc(start).normalize(), _utc(end).normalize()
    if s >= pd.Timestamp(_CAL_START, tz="UTC") and e <= pd.Timestamp(_CAL_END, tz="UTC"):
        full = _full_schedule()
        return full[(full.index >= s) & (full.index <= e)].copy()
    return _compute_schedule(s.tz_localize(None), e.tz_localize(None))


def nyse_sessions(start, end) -> pd.DatetimeIndex:
    idx = nyse_schedule(start, end).index
    return pd.DatetimeIndex(idx, name="ts")


def normalize_index(df: pd.DataFrame, timeframe: Timeframe) -> pd.DataFrame:
    """Copy with a sorted tz-aware UTC DatetimeIndex named `ts` (ns). Daily/weekly bars are mapped to their
    session date at 00:00 UTC (a local-midnight exchange timestamp keeps its local calendar date)."""
    out = df.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if timeframe.intraday:
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    else:
        local = idx if idx.tz is None else idx.tz_localize(None)  # wall-clock date in the source tz
        idx = local.normalize().tz_localize("UTC")
    out.index = idx.as_unit("ns").rename("ts")
    out.columns = [str(c).lower() for c in out.columns]
    return out.sort_index(kind="stable")


def _session_close_map(dates: pd.DatetimeIndex) -> pd.Series:
    if len(dates) == 0:
        return pd.Series(dtype="datetime64[ns, UTC]")
    sched = nyse_schedule(dates.min(), dates.max())
    return sched["market_close"]


def available_at(index: pd.DatetimeIndex, timeframe: Timeframe) -> pd.Series:
    """When each bar is complete. Bars outside any NYSE session get NaT (never visible)."""
    if len(index) == 0:
        return pd.Series(pd.DatetimeIndex([], tz="UTC").as_unit("ns"), index=index, name="available_at")
    if timeframe is Timeframe.D1:
        vals = pd.DatetimeIndex(_session_close_map(index).reindex(index))
    elif timeframe is Timeframe.W1:
        sched = nyse_schedule(index.min(), index.max() + pd.Timedelta(days=7))
        last_close = sched["market_close"].groupby(sched.index.tz_localize(None).to_period("W-FRI")).max()
        vals = pd.DatetimeIndex(last_close.reindex(index.tz_localize(None).to_period("W-FRI")))
    else:
        dates = index.tz_convert("America/New_York").tz_localize(None).normalize().tz_localize("UTC")
        closes = pd.DatetimeIndex(_session_close_map(dates).reindex(dates))
        end = pd.DatetimeIndex(index + timeframe.duration)
        vals = end.where(end < closes, closes)  # NaT close (non-session) -> NaT
        vals = vals.where(closes.notna())
    vals = vals.tz_localize("UTC") if vals.tz is None else vals.tz_convert("UTC")
    out = pd.Series(vals.as_unit("ns"), index=index, name="available_at")
    return out.astype("datetime64[ns, UTC]")


def to_canonical(df: pd.DataFrame, timeframe: Timeframe) -> pd.DataFrame:
    """Canonical bar frame: UTC index `ts`, float OHLCV, `available_at`. Extra columns are kept.
    Does NOT validate; production data goes through qsts.data.quality.validate_and_clean."""
    out = normalize_index(df, timeframe)
    for c in OHLCV:
        out[c] = out[c].astype("float64")
    out["available_at"] = available_at(out.index, timeframe)
    return out


def _agg(group: pd.DataFrame) -> dict:
    return {"open": group["open"].iloc[0], "high": group["high"].max(), "low": group["low"].min(),
            "close": group["close"].iloc[-1], "volume": group["volume"].sum()}


def resample_intraday_to_4h(h1: pd.DataFrame) -> pd.DataFrame:
    """Session-aligned 4H bars (09:30-13:30 and 13:30-close ET) built only from COMPLETE groups of 1H bars:
    a group is emitted only if its last 1H bar ends exactly at the segment's scheduled end."""
    if h1.empty:
        return h1.copy()
    local = h1.index.tz_convert("America/New_York")
    date = local.tz_localize(None).normalize()
    second = (local.hour * 60 + local.minute) >= 13 * 60 + 30
    closes = _session_close_map(pd.DatetimeIndex(date).tz_localize("UTC"))
    rows, idx = [], []
    for (d, seg), g in h1.groupby([date, second], sort=True):
        d_utc = pd.Timestamp(d).tz_localize("UTC")
        if d_utc not in closes.index:
            continue
        close_t = closes[d_utc]
        seg_end = close_t if seg else min(pd.Timestamp(d).tz_localize("America/New_York") + pd.Timedelta(hours=13, minutes=30),
                                          close_t).tz_convert("UTC")
        if g["available_at"].iloc[-1] != seg_end:
            continue  # incomplete group: never emit a partial 4H bar
        r = _agg(g)
        r["available_at"] = g["available_at"].iloc[-1]
        rows.append(r)
        idx.append(g.index[0])
    out = pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="ts"))
    out["available_at"] = out["available_at"].astype("datetime64[ns, UTC]")
    return out


def resample_daily_to_weekly(d1: pd.DataFrame) -> pd.DataFrame:
    """Weekly (Mon-Fri) bars from daily bars. available_at = close of the week's LAST scheduled session, and a
    trailing week whose last scheduled session is missing from the data is dropped (it is not complete)."""
    if d1.empty:
        return d1.copy()
    week = d1.index.tz_localize(None).to_period("W-FRI")
    sched = nyse_schedule(d1.index.min(), d1.index.max() + pd.Timedelta(days=7))
    week_close = sched["market_close"].groupby(sched.index.tz_localize(None).to_period("W-FRI")).max()
    rows, idx = [], []
    groups = list(d1.groupby(week, sort=True))
    for i, (w, g) in enumerate(groups):
        end = week_close.get(w)
        if end is None:
            continue
        last_session = sched.index[sched["market_close"] == end][0]
        if i == len(groups) - 1 and g.index[-1] < last_session:
            continue
        r = _agg(g)
        r["available_at"] = end
        rows.append(r)
        idx.append(g.index[0])
    out = pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="ts"))
    out["available_at"] = out["available_at"].astype("datetime64[ns, UTC]")
    return out


def align_higher_timeframe(decision_times: pd.Series, higher: pd.DataFrame, columns: list[str],
                           suffix: str) -> pd.DataFrame:
    """For each decision time, the latest higher-timeframe bar with available_at <= decision time (as-of join).
    This is the ONLY sanctioned way to combine timeframes."""
    t = pd.DatetimeIndex(decision_times)
    t = (t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")).as_unit("ns")
    left = pd.DataFrame({"t": t,
                         "_pos": range(len(decision_times))})
    right = higher[["available_at", *columns]].dropna(subset=["available_at"]).copy()
    right["available_at"] = pd.DatetimeIndex(right["available_at"]).tz_convert("UTC").as_unit("ns")
    right = right.sort_values("available_at", kind="stable")
    merged = pd.merge_asof(left.sort_values("t", kind="stable"), right, left_on="t", right_on="available_at",
                           direction="backward", allow_exact_matches=True).sort_values("_pos")
    out = merged[columns].rename(columns={c: f"{c}{suffix}" for c in columns})
    out.index = decision_times.index
    return out
