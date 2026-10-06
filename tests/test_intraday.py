"""Intraday ORB lab. All bars are SYNTHETIC (random walks or hand-made days), never market data."""
import itertools

import numpy as np
import pandas as pd
import pytest

from qsts.data.bars import Timeframe, nyse_schedule, to_canonical
from qsts.db import models as m
from qsts.research import intraday as itd
from qsts.research.intraday import IntradayLab, ORBRule, prepare_symbol, simulate_symbol


def synthetic_intraday(start, end, seed=0, bar_min=5, vol=0.002):
    """Random-walk bars for every NYSE session in [start, end] (half days included)."""
    rng = np.random.default_rng(seed)
    sched = nyse_schedule(start, end)
    stamps = []
    for o, c in zip(sched["market_open"], sched["market_close"]):
        stamps.append(pd.date_range(o, c, freq=f"{bar_min}min", inclusive="left"))
    idx = stamps[0].append(stamps[1:]) if len(stamps) > 1 else stamps[0]
    close = 100 * np.exp(np.cumsum(rng.normal(0, vol, len(idx))))
    open_ = np.r_[100.0, close[:-1]]
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 3, len(idx))))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 3, len(idx))))
    vol_ = rng.integers(1_000, 5_000, len(idx)).astype(float)
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": vol_}, index=idx)


def synthetic_daily_frame(start, end, seed=0):
    rng = np.random.default_rng(seed)
    idx = nyse_schedule(start, end).index
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, len(idx))))
    d = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx)
    return to_canonical(d, Timeframe.D1)


def universe(n, start, end, bar_min=5):
    bars = {f"S{i}": synthetic_intraday(start, end, seed=i, bar_min=bar_min) for i in range(n)}
    daily = {f"S{i}": synthetic_daily_frame("2023-01-01", end, seed=50 + i) for i in range(n)}
    return bars, daily


# ---------------------------------------------------------------------- hand-made days
def _day(date, rows):
    """5-minute bars of one session: `rows` = list of (o, h, l, c) for the first bars; the rest repeat the last close."""
    sched = nyse_schedule(date, date)
    stamps = pd.date_range(sched["market_open"].iloc[0], sched["market_close"].iloc[0], freq="5min", inclusive="left")
    last = rows[-1][3]
    full = rows + [(last, last, last, last)] * (len(stamps) - len(rows))
    return pd.DataFrame(full, columns=["open", "high", "low", "close"], index=stamps).assign(volume=1000.0)


LONG_DAY = [(100, 101, 99.5, 100.5), (100.5, 100.8, 99.8, 100.2), (100.2, 100.6, 99.6, 100.0),  # range 99.5-101
            (100.0, 101.4, 99.9, 101.2),   # closes above the range
            (101.3, 102.0, 101.0, 101.8),  # entry at this open (close confirmation)
            (101.8, 104.2, 101.7, 103.0)]  # then flat at 103 until the close
SHORT_DAY = [(104, 104.5, 103.5, 104), (104, 104.2, 103.8, 104), (104, 104.1, 103.9, 104),  # range 103.5-104.5
             (104, 104, 103.0, 103.2),      # closes below the range
             (103.1, 103.3, 102.9, 103.0),  # short entry at 103.1
             (103.0, 105.0, 102.95, 104.8)]  # reaches the stop (104.5)


def _two_days(short_day=SHORT_DAY):
    return pd.concat([_day("2025-03-03", LONG_DAY), _day("2025-03-04", short_day)])


def test_hand_made_breakouts_follow_the_rules_exactly():
    sd = prepare_symbol(_two_days(), None, 5)
    assert sd.c.shape == (2, 78) and list(sd.last_col) == [77, 77]
    assert np.isnan(sd.gap[0]) and sd.gap[1] == pytest.approx(104 / 103 - 1)  # previous session's last close
    t = simulate_symbol(sd, ORBRule(range_min=15, direction="both", confirm="close", stop="range", cutoff_min=60), 5, 0.0)
    assert list(t["direction"]) == [1.0, -1.0] and list(t["entry_min"]) == [20, 20]
    long_, short = t.iloc[0], t.iloc[1]
    assert long_["net"] == pytest.approx((103 - 101.3) / 101.3) and long_["r"] == pytest.approx(1.7 / 1.8)
    assert long_["exit"] == 0  # held to the close
    assert short["net"] == pytest.approx(-(104.5 - 103.1) / 103.1) and short["r"] == pytest.approx(-1.0)
    assert short["exit"] == 1 and short["risk_frac"] == pytest.approx(1.4 / 103.1)
    # profit target 1R: the long day reaches 101.3 + 1.8 = 103.1 in bar 5 (high 104.2)
    t1 = simulate_symbol(sd, ORBRule(range_min=15, direction="long", target_r=1.0, cutoff_min=60), 5, 0.0)
    assert t1["r"].iloc[0] == pytest.approx(1.0) and t1["exit"].iloc[0] == 2
    assert t1["entry_min"].iloc[1] == 30 and t1["r"].iloc[1] == 0  # day 2: a late close above the range, held flat
    # costs on both sides
    tc = simulate_symbol(sd, ORBRule(range_min=15, direction="long", cutoff_min=60), 5, 10.0)
    assert tc["net"].iloc[0] == pytest.approx((103 - 101.3) / 101.3 - 0.002)
    # stop order at the range edge: filled at 101 inside bar 3 (its low 99.9 stays above the stop)
    tt = simulate_symbol(sd, ORBRule(range_min=15, direction="long", confirm="touch", cutoff_min=60), 5, 0.0)
    assert tt["entry_min"].iloc[0] == 15 and tt["r"].iloc[0] == pytest.approx(2 / 1.5)
    # a gap through the stop fills at the (worse) open
    gapped = SHORT_DAY[:5] + [(104.8, 105.0, 104.7, 104.9)]
    tg = simulate_symbol(prepare_symbol(_two_days(gapped), None, 5), ORBRule(range_min=15, direction="short", cutoff_min=60), 5, 0.0)
    assert tg["net"].iloc[0] == pytest.approx(-(104.8 - 103.1) / 103.1)
    # no entries after the cut-off: the long breakout closes in bar 3 (minute 15), the window of 5 minutes ends there
    assert len(simulate_symbol(sd, ORBRule(range_min=15, direction="long", cutoff_min=5), 5, 0.0)) == 1
    late = LONG_DAY[:3] + [(100, 100.9, 99.9, 100.5)] * 3 + [(100.5, 101.6, 100.4, 101.5), (101.5, 101.6, 101.4, 101.5)]
    assert len(simulate_symbol(prepare_symbol(_day("2025-03-03", late), None, 5),
                               ORBRule(range_min=15, direction="long", cutoff_min=15), 5, 0.0)) == 0


def test_a_day_still_in_progress_is_not_a_whole_day():
    bars = _two_days()
    partial = bars[bars.index < pd.Timestamp("2025-03-04 17:00", tz="UTC")]  # second day cut at 12:00 New York
    sd = prepare_symbol(partial, None, 5)
    assert len(sd.sessions) == 1 and str(sd.sessions[0].date()) == "2025-03-03"
    half_day = _day("2025-11-28", LONG_DAY)  # the day after Thanksgiving closes at 13:00: a whole (short) day
    assert len(prepare_symbol(half_day, None, 5).sessions) == 1


def test_ambiguous_bars_are_resolved_against_the_strategy():
    # one bar breaks both edges: with stop orders the order is unknown -> no trade
    both = LONG_DAY[:3] + [(100.0, 101.5, 99.0, 100.0)] + [(100.0, 100.2, 99.9, 100.0)] * 2
    sd = prepare_symbol(_day("2025-03-03", both), None, 5)
    assert len(simulate_symbol(sd, ORBRule(range_min=15, confirm="touch", cutoff_min=60), 5, 0.0)) == 0
    # the entry bar also reaches the stop: counted as stopped
    whip = LONG_DAY[:3] + [(100.0, 101.5, 99.4, 101.0)] + [(101.0, 101.2, 100.9, 101.0)] * 2
    t = simulate_symbol(prepare_symbol(_day("2025-03-03", whip), None, 5),
                        ORBRule(range_min=15, direction="long", confirm="touch", cutoff_min=60), 5, 0.0)
    assert t["r"].iloc[0] == pytest.approx(-1.0) and t["exit"].iloc[0] == 1
    # stop and target in the same bar: the stop is assumed first
    wide = LONG_DAY[:5] + [(101.8, 110.0, 90.0, 101.0)]
    t = simulate_symbol(prepare_symbol(_day("2025-03-03", wide), None, 5),
                        ORBRule(range_min=15, direction="long", target_r=1.0, cutoff_min=60), 5, 0.0)
    assert t["exit"].iloc[0] == 1 and t["r"].iloc[0] == pytest.approx(-1.0)


def test_daily_context_comes_from_the_previous_session_only():
    bars = synthetic_intraday("2025-01-02", "2025-03-31", seed=3)
    daily = synthetic_daily_frame("2024-06-01", "2025-03-31", seed=4)
    sd = prepare_symbol(bars, daily, 5)
    d = daily.copy()
    prev = d["close"].shift()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(), (d["low"] - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean() / d["close"]
    trend = d["close"] / d["close"].rolling(50).mean() - 1
    i = 30
    day_before = d.index[d.index.get_loc(sd.sessions[i]) - 1]
    assert sd.atr_pct[i] == pytest.approx(atr[day_before]) and sd.trend[i] == pytest.approx(trend[day_before])


def test_trades_do_not_depend_on_later_data():
    """Look-ahead check: cutting all data (intraday and daily) after a day leaves every earlier trade unchanged."""
    bars = synthetic_intraday("2025-01-02", "2025-06-30", seed=5)
    daily = synthetic_daily_frame("2024-06-01", "2025-06-30", seed=6)
    cut = pd.Timestamp("2025-04-15", tz="UTC")
    rules = [ORBRule(), ORBRule(confirm="touch", stop="mid", target_r=2.0, min_rel_vol=1.0),
             ORBRule(range_min=30, direction="long", trend="with", min_range_atr=0.1, gap_min=0.0),
             ORBRule(range_min=5, direction="short", trend="against", max_range_atr=1.0, gap_max=0.0)]
    full_sd = prepare_symbol(bars, daily, 5)
    cut_sd = prepare_symbol(bars[bars.index < cut], daily[daily.index < cut], 5)
    for rule in rules:
        a = simulate_symbol(full_sd, rule, 5, 10.0)
        b = simulate_symbol(cut_sd, rule, 5, 10.0)
        pd.testing.assert_frame_equal(a[a["session"] < cut].reset_index(drop=True), b.reset_index(drop=True))


def test_no_edge_on_a_random_walk_without_costs():
    """Signals that only use the past cannot make money on a random walk: close-confirmed rules average ~0."""
    frames = [prepare_symbol(synthetic_intraday("2024-01-02", "2025-06-30", seed=10 + i), None, 5) for i in range(4)]
    for rng, side, stop, tgt in itertools.product([5, 30], ["long", "short"], ["range", "mid"], [0.0, 2.0]):
        rule = ORBRule(range_min=rng, direction=side, confirm="close", stop=stop, target_r=tgt)
        net = pd.concat([simulate_symbol(sd, rule, 5, 0.0) for sd in frames])["net"]
        assert len(net) > 500
        assert abs(net.mean() / net.std() * np.sqrt(len(net))) < 3.0, rule


# ---------------------------------------------------------------------- the lab
def test_boundary_is_fixed_and_the_search_never_sees_it(sf):
    bars, daily = universe(6, "2025-01-02", "2025-12-31")
    sess = nyse_schedule("2025-01-02", "2025-12-31").index
    cut = lambda n: {s: b[b.index < sess[n]] for s, b in bars.items()}  # noqa: E731
    lab = IntradayLab(sf, "5m", cut(100), daily, seed=1)
    assert lab.epoch == 1 and lab.oos_start == sess[75]
    assert all(d.sessions.max() < lab.oos_start for d in lab.days.values())
    assert len(lab.research_sessions) == 75 and len(lab.pre_sessions) == 15 and not lab.can_validate
    more = IntradayLab(sf, "5m", cut(150), daily, seed=1)  # more data later: the boundary does not move
    assert (more.epoch, more.oos_start) == (1, sess[75]) and len(more.oos_sessions) == 75
    doubled = IntradayLab(sf, "5m", cut(200), daily, seed=1)  # history doubled: new epoch over fresh sessions
    assert doubled.epoch == 2 and doubled.oos_start == sess[150] and doubled.universe_id != lab.universe_id
    assert [b["epoch"] for b in itd.boundaries(sf)] == [1, 2]
    assert IntradayLab(sf, "1h", {"S0": synthetic_intraday("2025-01-02", "2025-03-31", bar_min=60)}, daily).epoch == 1


def test_search_cycle_stores_every_trial_and_final_test_runs_once(sf):
    bars, daily = universe(6, "2024-10-01", "2025-12-31")
    logs = []
    lab = IntradayLab(sf, "5m", bars, daily, seed=2, log=logs.append)
    assert lab.can_validate and len(lab.oos_sessions) >= itd.MIN_OOS_SESSIONS
    out = lab.run_cycle(1, n_random=6, n_mutants=4)
    with sf() as s:
        n = s.query(m.IntradayCandidate).count()
    assert n == out["new_trials"] == lab.session_trials > 0 and logs
    with sf() as s:
        again = itd.rule_from_dict(s.query(m.IntradayCandidate).first().rule)
    lab.evaluate_and_store(again, "random", 2)  # an already tried rule is not stored (or counted) twice
    with sf() as s:
        assert s.query(m.IntradayCandidate).count() == n
    lb = lab.leaderboard()
    assert lb["rows"] and lb["n_trials"] >= n and lb["sessions"]["oos"] == len(lab.oos_sessions)
    row = lb["rows"][0]
    assert "Rango de los primeros" in row["rules"] and row["final"] is None
    curve = lab.curve(row["id"])
    assert curve["includes_oos"] is False and pd.Timestamp(curve["equity"][-1]["time"], unit="s", tz="UTC") < lab.oos_start
    with pytest.raises(ValueError, match="validación"):
        lab.final_test(row["id"])
    v = lab.validate(row["id"])
    assert set(v["gates"]) == {"min_trades", "costs_2x", "robustness", "pre_exam"}
    with sf() as s, s.begin():  # force it through to exercise the one-time test
        s.get(m.IntradayCandidate, row["id"]).status = "VALIDATED_PASS"
    f = lab.final_test(row["id"])
    assert f["decision"] in ("FINAL_PASS", "FINAL_FAIL") and set(f["checks"]) == {"positive", "beats_passive", "limited_decay"}
    assert f["period"][0] == str(lab.oos_start.date())
    assert lab.curve(row["id"])["includes_oos"] is True
    with pytest.raises(ValueError, match="ya tuvo"):
        with sf() as s, s.begin():
            s.get(m.IntradayCandidate, row["id"]).status = "VALIDATED_PASS"
        lab.final_test(row["id"])
    with sf() as s:
        assert s.query(m.OOSAccessLog).count() == 1


def test_random_and_mutated_rules_stay_inside_the_space(sf):
    bars, daily = universe(4, "2025-01-02", "2025-06-30")
    lab = IntradayLab(sf, "5m", bars, daily, seed=3)
    space = {**itd.COMMON, **itd.SPACE["5m"]}
    for _ in range(50):
        r = lab.mutate(lab.random_rule())
        assert r.valid() and all(getattr(r, k) in v for k, v in space.items())
        assert itd.rule_from_dict(r.to_dict()) == r and r.version_id == itd.rule_from_dict(r.to_dict()).version_id
    assert ORBRule().complexity() == 0 and ORBRule(target_r=2.0, trend="with").complexity() == 2


def test_intraday_endpoints(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from qsts.api.server import create_app
    from qsts.app.context import build_context
    from qsts.config import Settings
    from qsts.data.providers.csv_provider import CSVProvider
    ctx = build_context(Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/q.db", state_dir=tmp_path / "var"))
    root = tmp_path / "csv"
    (root / "1d").mkdir(parents=True)
    (root / "5m").mkdir()
    for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"]):
        d = synthetic_daily_frame("2024-01-02", "2025-06-30", seed=i)[["open", "high", "low", "close", "volume"]]
        d.index.name = "ts"
        d.to_csv(root / "1d" / f"{s}.csv")
        b = synthetic_intraday("2025-03-03", "2025-06-30", seed=i)
        b.index.name = "ts"
        b.to_csv(root / "5m" / f"{s}.csv")
    ctx.extra["data_provider_factory"] = lambda: CSVProvider(root)
    c = TestClient(create_app(ctx))

    def wait(path, key="running"):
        for _ in range(600):
            j = c.get(path).json()
            if not j[key]:
                return j
            time.sleep(0.1)
        raise AssertionError(f"{path} did not finish")
    assert c.post("/api/intraday/ingest", json={"dataset": "5m"}).status_code == 400  # no daily data yet
    assert c.post("/api/data/ingest", json={"mode": "symbols", "symbols": ["AAA", "BBB", "CCC", "DDD"]}).json()["started"]
    wait("/api/data/job")
    assert c.get("/api/intraday/leaderboard?dataset=5m").status_code == 400  # no intraday bars yet
    r = c.post("/api/intraday/ingest", json={"dataset": "5m", "n_symbols": 3}).json()
    assert r["started"] and r["symbols"] == 3
    j = wait("/api/data/job")
    assert j["ok"] == 3, j
    data = c.get("/api/intraday/data").json()["datasets"]
    assert data["5m"]["symbols"] == 3 and data["1h"]["symbols"] == 0 and data["5m"]["last"] == "2025-06-30"
    assert c.post("/api/intraday/start", json={"dataset": "2m"}).status_code == 400
    assert c.post("/api/intraday/start", json={"dataset": "5m", "max_cycles": 1}).json()["started"]
    st = wait("/api/intraday/status")
    assert st["error"] is None and any("ciclo 1" in x for x in st["log"]), st["log"]
    assert [b["dataset"] for b in st["boundaries"]] == ["5m"]
    lb = c.get("/api/intraday/leaderboard?dataset=5m").json()
    assert lb["rows"] and lb["symbols"] == 3 and lb["can_validate"] is False
    rid = lb["rows"][0]["id"]
    assert c.get(f"/api/intraday/{rid}/curve?dataset=5m").json()["equity"]
    assert c.post(f"/api/intraday/{rid}/final-test?dataset=5m").status_code == 400
    assert c.get("/api/intraday/nope/curve?dataset=5m").status_code == 404
