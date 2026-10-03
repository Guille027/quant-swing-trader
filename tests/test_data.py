import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily, synthetic_hourly
from qsts.data.adjust import adjust
from qsts.data.bars import (Timeframe, align_higher_timeframe, resample_daily_to_weekly,
                            resample_intraday_to_4h, to_canonical)
from qsts.data.providers.csv_provider import CSVProvider
from qsts.data.quality import DataQualityError, ValidatedBars, validate_and_clean
from qsts.data.repository import MarketDataRepository


def test_validated_bars_cannot_be_forged(daily):
    with pytest.raises(TypeError):
        ValidatedBars(daily, "X", Timeframe.D1, None)


def test_clean_daily_passes(daily):
    vb = validate_and_clean(daily, "X", Timeframe.D1)
    assert vb.report.ok and len(vb) == len(daily)
    assert (vb.df["available_at"].dt.hour.isin([20, 21, 18, 17, 19])).all()  # session close in UTC


def test_daily_available_at_after_close(daily):
    vb = validate_and_clean(daily, "X", Timeframe.D1)
    df = vb.df
    assert (df["available_at"] > df.index).all()
    # early close (day after Thanksgiving 2021) is 13:00 ET = 18:00 UTC
    assert df.loc["2021-11-26", "available_at"] == pd.Timestamp("2021-11-26 18:00", tz="UTC")


def test_duplicates_removed(daily):
    d = pd.concat([daily, daily.iloc[[5]]])
    vb = validate_and_clean(d, "X", Timeframe.D1)
    assert "DUPLICATES" in vb.report.codes() and len(vb) == len(daily)


def test_conflicting_duplicates_rejected(daily):
    dup = daily.iloc[[5]].copy()
    dup["close"] *= 1.1
    dup["high"] = dup[["high", "close"]].max(axis=1)
    with pytest.raises(DataQualityError) as e:
        validate_and_clean(pd.concat([daily, dup]), "X", Timeframe.D1)
    assert "CONFLICTING_DUPLICATES" in e.value.report.codes()


def test_impossible_ohlc_removed(daily):
    d = daily.copy()
    d.iloc[10, d.columns.get_loc("high")] = d.iloc[10]["low"] * 0.5
    d.iloc[11, d.columns.get_loc("close")] = -1
    vb = validate_and_clean(d, "X", Timeframe.D1)
    assert "IMPOSSIBLE_OHLC" in vb.report.codes()
    # removing 2 of ~750 sessions -> warning only
    assert len(vb) == len(d) - 2


def test_missing_sessions_detected(daily):
    d = daily.drop(daily.index[100:140])
    with pytest.raises(DataQualityError) as e:
        validate_and_clean(d, "X", Timeframe.D1)
    assert "MISSING_SESSIONS" in e.value.report.codes()


def test_missing_sessions_not_filled(daily):
    d = daily.drop(daily.index[[50, 51]])
    vb = validate_and_clean(d, "X", Timeframe.D1)
    assert len(vb) == len(d) and "MISSING_SESSIONS" in vb.report.codes()


def test_weekend_bar_rejected(daily):
    extra = daily.iloc[[0]].copy()
    extra.index = pd.DatetimeIndex([pd.Timestamp("2020-01-04", tz="UTC")])  # Saturday
    vb = validate_and_clean(pd.concat([daily, extra]), "X", Timeframe.D1)
    assert "NON_SESSION_BARS" in vb.report.codes()


def test_stale_and_future(daily):
    with pytest.raises(DataQualityError) as e:
        validate_and_clean(daily, "X", Timeframe.D1, asof=pd.Timestamp("2023-02-01", tz="UTC"))
    assert "STALE" in e.value.report.codes()
    with pytest.raises(DataQualityError) as e:
        validate_and_clean(daily, "X", Timeframe.D1, asof=pd.Timestamp("2022-06-01", tz="UTC"))
    assert "FUTURE_TIMESTAMPS" in e.value.report.codes()


def test_unadjusted_split_flagged(daily):
    d = daily.copy()
    d.loc[d.index >= "2021-06-01", ["open", "high", "low", "close"]] /= 4
    vb = validate_and_clean(d, "X", Timeframe.D1)
    assert "EXTREME_MOVE" in vb.report.codes()
    actions = pd.DataFrame({"ex_date": [pd.Timestamp("2021-06-01", tz="UTC")], "kind": ["split"], "value": [4.0]})
    adj = adjust(vb.df, actions, "split")
    lr = np.log(adj["close"]).diff().abs()
    assert lr.max() < 0.2
    assert np.allclose(adj["close"][adj.index >= "2021-06-01"], adj["raw_close"][adj.index >= "2021-06-01"])


def test_dividend_total_return(daily):
    actions = pd.DataFrame({"ex_date": [pd.Timestamp("2021-03-01", tz="UTC")], "kind": ["dividend"], "value": [1.0]})
    adj = adjust(daily, actions, "total")
    before = daily.index < "2021-03-01"
    prev = daily["close"][before].iloc[-1]
    assert np.allclose(adj["close"][before], daily["close"][before] * (1 - 1 / prev))
    assert np.allclose(adj["close"][~before], daily["close"][~before])


def test_4h_resample_and_mtf_alignment_no_lookahead():
    h1 = to_canonical(synthetic_hourly(), Timeframe.H1)
    h4 = resample_intraday_to_4h(h1)
    assert (h4.groupby(h4.index.normalize()).size() == 2).all()  # 7 hourly bars -> 4 + 3
    d1 = to_canonical(synthetic_daily("2024-02-01", "2024-03-29"), Timeframe.D1)
    aligned = align_higher_timeframe(h1["available_at"], d1, ["close"], "_d")
    # every hourly decision only sees a daily bar whose close time <= decision time
    for t, row in zip(h1["available_at"], aligned.itertuples()):
        visible = d1[d1["available_at"] <= t]
        assert row.close_d == visible["close"].iloc[-1]
    # intraday decision on day D must NOT see daily bar D
    first_hour = h1.iloc[0]
    day = first_hour.name.normalize()
    assert aligned["close_d"].iloc[0] != d1.loc[day, "close"]


def test_weekly_resample(daily):
    d = to_canonical(daily, Timeframe.D1)
    w = resample_daily_to_weekly(d)
    assert w["high"].max() == d["high"].max()
    assert (w["available_at"].dt.dayofweek <= 4).all()


def test_csv_provider_and_repository(tmp_path, sf, daily):
    (tmp_path / "1d").mkdir()
    out = daily.copy()
    out.index.name = "ts"
    out.to_csv(tmp_path / "1d" / "AAA.csv")
    p = CSVProvider(tmp_path)
    raw = p.get_bars("AAA", Timeframe.D1, "2020-01-01", "2022-12-31")
    vb = validate_and_clean(raw, "AAA", Timeframe.D1)
    repo = MarketDataRepository(sf)
    repo.upsert_asset("AAA", sector="Tech")
    assert repo.store_bars(vb, "csv") == len(daily)
    assert repo.store_bars(vb, "csv") == len(daily)  # idempotent re-store
    back = repo.load_bars("AAA", Timeframe.D1)
    assert len(back) == len(daily)
    assert np.allclose(back["close"].values, daily["close"].values)
    with pytest.raises(TypeError):
        repo.store_bars(raw, "csv")


def test_universe_point_in_time(sf):
    from datetime import date
    repo = MarketDataRepository(sf)
    for s in ("OLD", "NEW", "STAY"):
        repo.upsert_asset(s)
    repo.add_membership("SP500", "OLD", date(2010, 1, 1), date(2015, 6, 1))
    repo.add_membership("SP500", "NEW", date(2015, 6, 1), None)
    repo.add_membership("SP500", "STAY", date(2000, 1, 1), None)
    assert repo.universe_asof("SP500", date(2012, 1, 1)) == ["OLD", "STAY"]
    assert repo.universe_asof("SP500", date(2020, 1, 1)) == ["NEW", "STAY"]
    assert repo.has_history("SP500")


def test_point_in_time_fundamentals(sf):
    from datetime import datetime, date
    from qsts.data.repository import PointInTimeStore
    from qsts.db import models as m
    repo = MarketDataRepository(sf)
    aid = repo.upsert_asset("AAA")
    with sf() as s, s.begin():
        s.add(m.Fundamental(asset_id=aid, period_end=date(2021, 3, 31), metric="eps", value=1.0,
                            available_at=datetime(2021, 4, 28, 20, 5)))
        s.add(m.News(asset_id=aid, published_at=datetime(2021, 4, 28, 15), available_at=datetime(2021, 4, 28, 15),
                     source="x", headline="h"))
    pit = PointInTimeStore(sf)
    assert pit.fundamentals_asof(aid, "2021-04-28 19:00") == {}
    assert pit.fundamentals_asof(aid, "2021-04-28 21:00") == {"eps": 1.0}
    assert pit.news_asof(aid, "2021-04-28 14:00") == []
    assert len(pit.news_asof(aid, "2021-04-28 15:00")) == 1
