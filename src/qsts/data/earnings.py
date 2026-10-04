"""Quarterly earnings events -> point-in-time columns attached to daily bars.

Timing (all session arithmetic uses the NYSE calendar):
- info session: the first session whose CLOSE is strictly after the announcement. Decisions are taken at closes,
  so that is the first decision that may use the reported EPS / surprise. Results at "4 PM" (after the close) are
  therefore usable from the next session; results at "8 AM" (before the open) from the same session.
- impact session: the first session whose OPEN is after the announcement: the open where the price gap happens.
- Unknown time (Yahoo shows 12 AM): both conservative choices at once: info = next session, impact = same day.

Columns:
- earn_days_since: sessions since the last info session (0 on it); NaN before the first known event.
- earn_surprise: surprise (%) of the last event whose info session is <= t; NaN if unknown.
- earn_days_to: sessions from t to the next impact session (1 = the gap would be at the next open), or
  HORIZON + 1 when none is due within HORIZON sessions; NaN when the symbol has no earnings data at all.
  ASSUMPTION: scheduled dates are known HORIZON sessions ahead (companies usually confirm 2-4 weeks before).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from qsts.data.bars import nyse_schedule

HORIZON = 10
EARN_COLS = ["earn_days_since", "earn_surprise", "earn_days_to"]


def _sessions_around(index: pd.DatetimeIndex, events: pd.DataFrame) -> pd.DataFrame:
    lo = min(index.min(), events["announced_at"].min()) - pd.Timedelta(days=10)
    hi = max(index.max(), events["announced_at"].max()) + pd.Timedelta(days=40)
    return nyse_schedule(lo, hi)


def event_sessions(events: pd.DataFrame, sched: pd.DataFrame) -> pd.DataFrame:
    """Adds info_session / impact_session (session dates, UTC midnight) to each event."""
    out = events.copy()
    ann = pd.DatetimeIndex(out["announced_at"]).tz_convert("UTC")
    known = out["time_known"].astype(bool).to_numpy() if "time_known" in out else np.ones(len(out), dtype=bool)
    local_day = ann.tz_convert("America/New_York").tz_localize(None).normalize()
    end_of_day = (local_day + pd.Timedelta(hours=23, minutes=59)).tz_localize("America/New_York").tz_convert("UTC")
    start_of_day = local_day.tz_localize("America/New_York").tz_convert("UTC")
    info_t = np.where(known, ann, end_of_day)    # unknown time: assume after the close (later information)
    impact_t = np.where(known, ann, start_of_day)  # unknown time: assume before the open (earlier gap)
    closes, opens = sched["market_close"].to_numpy(), sched["market_open"].to_numpy()
    sess = sched.index
    i_info = np.searchsorted(closes, pd.DatetimeIndex(info_t).tz_convert("UTC").as_unit("ns").to_numpy(), side="right")
    i_imp = np.searchsorted(opens, pd.DatetimeIndex(impact_t).tz_convert("UTC").as_unit("ns").to_numpy(), side="right")
    out["info_session"] = [sess[i] if i < len(sess) else pd.NaT for i in i_info]
    out["impact_session"] = [sess[i] if i < len(sess) else pd.NaT for i in i_imp]
    return out


def earnings_columns(index: pd.DatetimeIndex, events: pd.DataFrame | None, horizon: int = HORIZON) -> pd.DataFrame:
    """PIT columns for bars dated by session (UTC midnight). `events`: announced_at (UTC), surprise_pct, time_known."""
    out = pd.DataFrame(np.nan, index=index, columns=EARN_COLS)
    if events is None or len(events) == 0 or len(index) == 0:
        return out
    ev = events.dropna(subset=["announced_at"])
    if ev.empty:
        return out
    sched = _sessions_around(index, ev)
    ev = event_sessions(ev, sched).sort_values("info_session", kind="stable")
    ordinal = pd.Series(np.arange(len(sched)), index=sched.index)
    pos = ordinal.reindex(index).to_numpy(dtype=float)  # session number of each bar (NaN if not a session)

    info = ev.dropna(subset=["info_session"])
    info_pos = ordinal.reindex(pd.DatetimeIndex(info["info_session"])).to_numpy(dtype=float)
    k = np.searchsorted(info_pos, pos, side="right") - 1  # last event with info_session <= t
    has = (k >= 0) & ~np.isnan(pos)
    kk = np.clip(k, 0, None)
    out["earn_days_since"] = np.where(has, pos - info_pos[kk], np.nan)
    surprise = info["surprise_pct"].to_numpy(dtype=float) if "surprise_pct" in info else np.full(len(info), np.nan)
    out["earn_surprise"] = np.where(has, surprise[kk], np.nan) if len(info) else np.nan

    imp = np.sort(ordinal.reindex(pd.DatetimeIndex(ev["impact_session"].dropna())).to_numpy(dtype=float))
    j = np.searchsorted(imp, pos, side="right")  # first impact session strictly after t
    nxt = np.where(j < len(imp), imp[np.clip(j, 0, len(imp) - 1)] if len(imp) else np.nan, np.nan)
    d = nxt - pos
    out["earn_days_to"] = np.where(np.isnan(pos), np.nan, np.where(np.isnan(d) | (d > horizon), horizon + 1, d))
    return out
