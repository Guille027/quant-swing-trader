"""Quarterly results: point-in-time timing, engine rules, provider parsing, storage. SYNTHETIC data only."""
import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.data.bars import Timeframe, nyse_sessions, to_canonical
from qsts.data.earnings import EARN_COLS, HORIZON, earnings_columns
from qsts.data.repository import MarketDataRepository


def ny(*ts):
    return pd.to_datetime(list(ts)).tz_localize("America/New_York").tz_convert("UTC")


EVENTS = pd.DataFrame({"announced_at": ny("2024-04-25 08:00", "2024-05-02 16:00", "2024-05-08 00:00"),
                       "surprise_pct": [-3.0, 5.0, 1.0], "time_known": [True, True, False]})


def test_timing_after_close_before_open_and_unknown():
    idx = nyse_sessions("2024-04-22", "2024-05-10")
    c = earnings_columns(idx, EVENTS)
    at = lambda d, col: c.loc[pd.Timestamp(d, tz="UTC"), col]  # noqa: E731
    # before the open (8 AM): gap at that day's open, usable at that day's close
    assert at("2024-04-24", "earn_days_to") == 1 and at("2024-04-25", "earn_days_since") == 0
    assert np.isnan(at("2024-04-24", "earn_surprise")) and at("2024-04-25", "earn_surprise") == -3.0
    # after the close (4 PM): gap at the next open, usable from the next close
    assert at("2024-05-02", "earn_days_to") == 1 and at("2024-05-02", "earn_surprise") == -3.0
    assert at("2024-05-03", "earn_surprise") == 5.0 and at("2024-05-03", "earn_days_since") == 0
    # unknown time (12 AM): gap assumed at that open AND information only from the next close
    assert at("2024-05-07", "earn_days_to") == 1 and at("2024-05-08", "earn_surprise") == 5.0
    assert at("2024-05-09", "earn_surprise") == 1.0
    assert at("2024-05-10", "earn_days_to") == HORIZON + 1  # nothing due within the horizon


def test_past_columns_never_use_later_events():
    idx = nyse_sessions("2024-04-22", "2024-06-28")
    base = earnings_columns(idx, EVENTS)
    later = pd.concat([EVENTS, pd.DataFrame({"announced_at": ny("2024-06-20 16:00"), "surprise_pct": [50.0],
                                             "time_known": [True]})], ignore_index=True)
    more = earnings_columns(idx, later)
    before = idx < pd.Timestamp("2024-06-21", tz="UTC")
    for col in ("earn_days_since", "earn_surprise"):
        pd.testing.assert_series_equal(base[col][before], more[col][before])
    # truncating the bars never changes the earlier values (prefix property)
    short = earnings_columns(idx[:20], EVENTS)
    pd.testing.assert_frame_equal(short, base.iloc[:20])
    assert earnings_columns(idx, None)[EARN_COLS].isna().all().all()


def _with_days_to(df, values):
    out = df.copy()
    out["earn_days_to"] = values
    return out


class _FakeTicker:
    """Mimics the documented output of yfinance get_earnings_dates (SYNTHETIC values)."""
    def get_earnings_dates(self, limit=12):
        idx = pd.DatetimeIndex([pd.Timestamp("2026-10-29 16:00"), pd.Timestamp("2026-07-24 16:00"),
                                pd.Timestamp("2026-04-23 00:00")]).tz_localize("America/New_York")
        idx.name = "Earnings Date"
        return pd.DataFrame({"EPS Estimate": [1.5, 1.2, 1.0], "Reported EPS": [np.nan, 1.3, 0.9],
                             "Surprise(%)": [np.nan, 8.33, -10.0]}, index=idx)


def test_yahoo_earnings_parsing(monkeypatch):
    pytest.importorskip("yfinance")
    from qsts.data.providers.yfinance_provider import YFinanceProvider
    p = YFinanceProvider()
    monkeypatch.setattr(p, "_ticker", lambda s: _FakeTicker())
    e = p.get_earnings("X")
    assert list(e["announced_at"]) == sorted(e["announced_at"]) and str(e["announced_at"].dt.tz) == "UTC"
    assert list(e["time_known"]) == [False, True, True]  # 12 AM = unknown time
    assert e["surprise_pct"].tolist()[1] == pytest.approx(8.33) and np.isnan(e["eps_reported"].iloc[-1])


def test_store_and_update_earnings(sf):
    repo = MarketDataRepository(sf)
    repo.upsert_asset("AAA")
    ev = EVENTS.assign(eps_estimate=1.0, eps_reported=np.nan)
    assert repo.store_earnings("AAA", ev, "test") == 3
    ev2 = ev.copy()
    ev2.loc[0, "eps_reported"] = 1.1
    repo.store_earnings("AAA", ev2, "test")  # later fetch fills the reported figure, no duplicates
    back = repo.load_earnings("AAA")
    assert len(back) == 3 and back["eps_reported"].iloc[0] == 1.1 and back["announced_at"].iloc[0] == EVENTS["announced_at"].iloc[0]
    assert repo.earnings_summary()["AAA"]["events"] == 3
