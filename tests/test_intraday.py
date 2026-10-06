"""Intraday ORB lab. All bars are SYNTHETIC (random walks or hand-made days), never market data."""
import itertools

import numpy as np
import pandas as pd
import pytest

from qsts.data.bars import Timeframe, nyse_schedule, to_canonical
from qsts.db import models as m
from qsts.research import intraday as itd
from qsts.research.intraday import GapRule, IntradayLab, ORBRule, VWAPRule, prepare_symbol, simulate_symbol


def synthetic_intraday(start, end, seed=0, bar_min=5, vol=0.002, gap_vol=0.006):
    """Random-walk bars for every NYSE session in [start, end] (half days included), with overnight gaps."""
    rng = np.random.default_rng(seed)
    sched = nyse_schedule(start, end)
    stamps = []
    for o, c in zip(sched["market_open"], sched["market_close"]):
        stamps.append(pd.date_range(o, c, freq=f"{bar_min}min", inclusive="left"))
    idx = stamps[0].append(stamps[1:]) if len(stamps) > 1 else stamps[0]
    eps = rng.normal(0, vol, len(idx))
    jump = np.zeros(len(idx))
    first = np.r_[0, np.cumsum([len(x) for x in stamps])[:-1]]
    jump[first] = rng.normal(0, gap_vol, len(first))  # the open differs from the previous close
    log_close = np.log(100) + np.cumsum(eps + jump)
    close = np.exp(log_close)
    open_ = np.exp(log_close - eps)
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


GAP_DAY = [(102, 102.3, 101.8, 102.0), (102.0, 102.1, 101.7, 101.8), (101.8, 101.9, 101.2, 101.4),  # opens +2%
           (101.4, 101.5, 100.5, 100.6),   # entry at this open (15 min)
           (100.6, 100.7, 99.8, 100.0)]    # back to yesterday's close (100): the gap is closed


def test_hand_made_gap_day():
    flat = [(100, 100, 100, 100)] * 2
    sd = prepare_symbol(pd.concat([_day("2025-03-03", flat), _day("2025-03-04", GAP_DAY)]), None, 5)
    assert sd.gap[1] == pytest.approx(0.02)
    rule = GapRule(mode="fade", min_gap=0.01, entry_min=15, stop_atr=0.5, target="fill")
    assert len(simulate_symbol(sd, rule, 5, 0.0)) == 0  # without the daily ATR there is no stop: no trade
    sd.atr_pct[:] = 0.02
    t = simulate_symbol(sd, rule, 5, 0.0)
    assert len(t) == 1 and t["direction"].iloc[0] == -1 and t["entry_min"].iloc[0] == 15 and t["exit"].iloc[0] == 2
    assert t["net"].iloc[0] == pytest.approx((101.4 - 100.0) / 101.4)
    assert t["r"].iloc[0] == pytest.approx(1.4 / (0.5 * 0.02 * 101.4))
    go = simulate_symbol(sd, GapRule(mode="go", min_gap=0.01, entry_min=15, stop_atr=0.5), 5, 0.0)
    assert go["direction"].iloc[0] == 1 and go["r"].iloc[0] == pytest.approx(-1.0) and go["exit"].iloc[0] == 1
    assert len(simulate_symbol(sd, GapRule(mode="go", min_gap=0.01, entry_min=15, confirm=True), 5, 0.0)) == 0
    assert len(simulate_symbol(sd, GapRule(mode="fade", min_gap=0.03), 5, 0.0)) == 0  # gap too small
    assert len(simulate_symbol(sd, GapRule(mode="fade", min_gap=0.01, side="down"), 5, 0.0)) == 0
    late = GapRule(mode="fade", min_gap=0.01, entry_min=30, target="fill")  # the gap closed before the entry
    assert len(simulate_symbol(sd, late, 5, 0.0)) == 0


VWAP_DAY = [(100, 100.2, 99.8, 100.0), (100.0, 100.1, 99.5, 99.6), (99.6, 99.7, 99.3, 99.4),
            (99.4, 100.0, 99.4, 99.9),     # closes back above the VWAP
            (99.95, 100.6, 99.9, 100.5)]   # entry at this open, then flat at 100.5


def test_hand_made_vwap_cross():
    sd = prepare_symbol(_day("2025-03-03", VWAP_DAY), None, 5)
    sd.atr_pct[:] = 0.02
    tp = np.array([(h + lo + c) / 3 for _, h, lo, c in VWAP_DAY])
    vwap = np.cumsum(tp) / np.arange(1, 6)  # equal volumes
    assert VWAP_DAY[3][3] > vwap[3] and VWAP_DAY[2][3] <= vwap[2]  # the cross happens in bar 3
    t = simulate_symbol(sd, VWAPRule(mode="cross", direction="long", start_min=15, stop_atr=0.5), 5, 0.0)
    assert len(t) == 1 and t["entry_min"].iloc[0] == 20 and t["exit"].iloc[0] == 0
    assert t["net"].iloc[0] == pytest.approx((100.5 - 99.95) / 99.95)
    assert len(simulate_symbol(sd, VWAPRule(mode="cross", direction="long", start_min=30), 5, 0.0)) == 0  # too early
    assert len(simulate_symbol(sd, VWAPRule(mode="cross", direction="short", start_min=15), 5, 0.0)) == 0


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
             ORBRule(range_min=5, direction="short", trend="against", max_range_atr=1.0, gap_max=0.0),
             GapRule(mode="fade", min_gap=0.001, target="fill", confirm=True), GapRule(mode="go", min_gap=0.001, target="2R"),
             VWAPRule(mode="cross", target="1R", trend="with"), VWAPRule(mode="revert", stretch_atr=0.25, target="vwap")]
    full_sd = prepare_symbol(bars, daily, 5)
    cut_sd = prepare_symbol(bars[bars.index < cut], daily[daily.index < cut], 5)
    for rule in rules:
        a = simulate_symbol(full_sd, rule, 5, 10.0)
        b = simulate_symbol(cut_sd, rule, 5, 10.0)
        assert len(a) > 20, rule
        pd.testing.assert_frame_equal(a[a["session"] < cut].reset_index(drop=True), b.reset_index(drop=True))


def test_gap_and_vwap_rules_have_no_edge_on_a_random_walk():
    frames = [prepare_symbol(synthetic_intraday("2024-01-02", "2025-06-30", seed=30 + i, vol=0.003),
                             synthetic_daily_frame("2023-01-02", "2025-06-30", seed=60 + i), 5) for i in range(4)]
    rules = [GapRule(mode=mo, min_gap=0.002, entry_min=e, target=tg) for mo, e, tg in
             itertools.product(["fade", "go"], [5, 30], ["close", "1R"])]
    rules += [GapRule(mode="fade", min_gap=0.002, target="fill")]
    rules += [VWAPRule(mode=mo, start_min=st, stretch_atr=0.25, target=tg) for mo, st, tg in
              itertools.product(["cross", "revert"], [15, 60], ["close", "2R"])]
    rules += [VWAPRule(mode="revert", stretch_atr=0.25, target="vwap")]
    for rule in rules:
        net = pd.concat([simulate_symbol(sd, rule, 5, 0.0) for sd in frames])["net"]
        assert len(net) > 300, rule
        assert abs(net.mean() / net.std() * np.sqrt(len(net))) < 3.0, rule


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
    assert row["family"] in itd.FAMILIES and len(row["rules"]) > 40 and row["final"] is None
    assert sum(lb["by_family"].values()) == lb["n_trials_here"] == n
    only_gap = lab.leaderboard(family="gap")["rows"]
    assert all(r["family"] == "gap" for r in only_gap)
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
    seen = set()
    for _ in range(150):
        r = lab.mutate(lab.random_rule())
        seen.add(r.family)
        space = itd.family_space(r.family, "5m")
        assert r.valid() and all(getattr(r, k) in v for k, v in space.items()) and r == r.canonical()
        assert itd.rule_from_dict(r.to_dict()) == r and r.version_id == itd.rule_from_dict(r.to_dict()).version_id
    assert seen == {"orb", "gap", "vwap"}
    assert ORBRule().complexity() == 0 and ORBRule(target_r=2.0, trend="with").complexity() == 2
    # the first ORB rules (stored before other families existed) keep their identity
    assert "family" not in ORBRule().to_dict() and itd.rule_from_dict(ORBRule().to_dict()) == ORBRule()
    assert not GapRule(mode="go", target="fill").valid() and not VWAPRule(mode="cross", target="vwap").valid()
    only = IntradayLab(sf, "5m", bars, daily, seed=3, families=("gap",))
    assert {only.random_rule().family for _ in range(20)} == {"gap"}


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
    fam = c.get("/api/intraday/leaderboard?dataset=5m&family=gap").json()
    assert all(r["family"] == "gap" for r in fam["rows"]) and set(fam["by_family"]) == {"orb", "gap", "vwap"}
    # day-by-day simulation of a frozen rule
    assert c.get("/api/intraday/paper").json()["active"] is False
    assert c.post("/api/intraday/paper/start", json={"rid": "nope", "dataset": "5m"}).status_code == 404
    assert c.post("/api/intraday/paper/start", json={"rid": rid, "dataset": "5m", "capital": 2363}).json()["session_id"]
    v = c.get("/api/intraday/paper").json()
    assert v["active"] is True and v["capital"] == 2363 and v["symbols"] == 3 and v["n_days"] == 0
    assert c.post("/api/intraday/paper/start", json={"rid": rid, "dataset": "5m"}).status_code == 400  # one at a time
    assert c.post("/api/intraday/paper/stop").json() == {"stopped": True}
    assert c.post("/api/intraday/paper/stop").status_code == 400


# ---------------------------------------------------------------------- day-by-day simulation of a frozen rule
def _paper_data(end="2025-06-30", n=5):
    bars = {f"S{i}": synthetic_intraday("2025-01-02", end, seed=70 + i) for i in range(n)}
    daily = {f"S{i}": synthetic_daily_frame("2024-01-02", end, seed=80 + i) for i in range(n)}
    return bars, daily


def test_intraday_paper_records_new_sessions_once(sf):
    from qsts.execution.intraday_paper import IntradayPaper
    from qsts.execution.paper import PaperError
    state = {"data": _paper_data("2025-05-30")}
    paper = IntradayPaper(sf, lambda ds, syms: ({s: state["data"][0][s] for s in syms}, state["data"][1]))
    rule = ORBRule(range_min=15, direction="both").to_dict()
    with pytest.raises(PaperError):
        paper.start(rule, "5m", ["S0"], 50)  # capital too small
    sid = paper.start(rule, "5m", [f"S{i}" for i in range(5)], 2363, "EUR", now=pd.Timestamp("2025-05-15 15:00", tz="UTC"))
    with pytest.raises(PaperError):
        paper.start(rule, "5m", ["S0"], 1000)  # one at a time
    ps = paper.active()
    assert ps.id == sid and str(ps.start) == "2025-05-16"  # 15:00 UTC: that day's session had already opened
    first = paper.update()
    assert [str(d.day) for d in first][0] == "2025-05-16" and str(first[-1].day) == "2025-05-30"
    eq = 2363.0
    for d in first:  # the account moves only by that day's trades
        assert d.equity == pytest.approx(eq + d.pnl) and sum(t["amount"] for t in d.trades) <= eq * 1.0001
        assert d.pnl == pytest.approx(sum(t["pnl"] for t in d.trades))
        eq = d.equity
    assert paper.update() == []  # nothing new: nothing recorded twice
    # data revised afterwards (re-download) + new sessions: recorded days stay as they were, only new ones are added
    revised = _paper_data("2025-06-30")
    revised[0]["S0"] = revised[0]["S0"] * 1.01
    state["data"] = revised
    more = paper.update()
    assert str(more[0].day) == "2025-06-02" and str(more[-1].day) == "2025-06-30"
    j = paper.journal(sid)
    assert [(d.day, d.equity) for d in j[:len(first)]] == [(d.day, d.equity) for d in first]
    assert more[0].equity == pytest.approx(first[-1].equity + more[0].pnl)
    v = paper.view(now=pd.Timestamp("2025-07-01 12:00", tz="UTC"))
    assert v["active"] and v["n_days"] == len(j) and v["equity"] == pytest.approx(j[-1].equity)
    assert v["return"] == pytest.approx(j[-1].equity / 2363 - 1) and v["days_waiting"] == 0
    assert v["equity_curve"][0]["value"] == 2363 and len(v["equity_curve"]) == len(j) + 1
    paper.stop()
    assert paper.active() is None and paper.view()["active"] is False and paper.view()["n_days"] == len(j)


def test_intraday_day_message_reads_well():
    from qsts.notify.report import intraday_day_message
    view = {"currency": "EUR", "dataset": "5m", "rule": "Rango de los primeros 15 min", "return": 0.012,
            "start": "2025-05-16", "n_days": 3}
    day = {"day": "2025-05-20", "equity": 2390.0, "pnl": 12.0, "passive": -0.004,
           "trades": [{"symbol": "AAPL", "side": "largo", "entry_min": 20, "exit": "objetivo", "net": 0.008, "pnl": 15.1},
                      {"symbol": "MSFT", "side": "corto", "entry_min": 45, "exit": "stop", "net": -0.005, "pnl": -3.1}]}
    msg = intraday_day_message(view, day)
    assert "martes 20 may" in msg and "AAPL largo a las 15:50" in msg and "objetivo" in msg  # 9:30 New York = 15:30 Madrid
    assert "+12,00 €" in msg and "2.390,00 €" in msg and "no son señales en directo" in msg
    assert "Hoy la regla no ha encontrado" in intraday_day_message(view, {**day, "trades": []})


def test_reporter_keeps_intraday_bars_fresh_and_sends_each_session(sf):
    from qsts.app.daily import DailyReporter
    from qsts.execution.intraday_paper import IntradayPaper
    data = _paper_data("2025-06-30")
    paper = IntradayPaper(sf, lambda ds, syms: ({s: data[0][s] for s in syms}, data[1]))
    paper.start(ORBRule().to_dict(), "5m", [f"S{i}" for i in range(5)], 2000, now=pd.Timestamp("2025-06-20", tz="UTC"))
    newest = {"t": pd.Timestamp("2025-06-26 19:55", tz="UTC")}
    started, sent = [], []

    class Runner:
        state = type("S", (), {"running": False})()
    class Tg:
        def send(self, text):
            sent.append(text)
    rep = DailyReporter(sf, paper=lambda: None, telegram=lambda: Tg(), data_runner=lambda: Runner(),
                        intraday_paper=lambda: paper, intraday_data=lambda: {"5m": (["S0", "S1"], newest["t"])},
                        intraday_update=lambda ds, syms: started.append((ds, syms)) or True)
    now = pd.Timestamp("2025-07-01 12:00", tz="UTC")  # latest finished session: 2025-06-30
    assert "descargando" in rep.intraday_tick(now) and started == [("5m", ["S0", "S1"])]
    assert "descargando" not in rep.intraday_tick(now) and len(started) == 1  # retries wait 20 minutes
    newest["t"] = pd.Timestamp("2025-06-30 19:55", tz="UTC")  # the download brought the latest session
    rep._attempts.clear()
    rep.intraday_tick(now)
    assert len(sent) == 3 and "Simulación intradía" in sent[-1] and "lunes 30 jun" in sent[-1]  # the last 3 sessions
    assert all(d.notified_at is not None for d in paper.journal(paper.active().id))  # older ones: in the app only
    rep.intraday_tick(now)
    assert len(sent) == 3 and len(started) == 1  # nothing twice
