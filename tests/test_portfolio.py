import numpy as np
import pandas as pd

from qsts.portfolio.engine import (AllocationConfig, ConfidenceCalibrator, allocate, concentration_report,
                                   correlation_clusters, decay_check, effective_bets, shrunk_covariance)

rng = np.random.default_rng(0)


def corr_returns(n=500):
    tech = rng.normal(0, 0.02, n)
    df = pd.DataFrame({s: tech + rng.normal(0, 0.006, n) for s in ("NVDA", "AMD", "AVGO", "TSM", "MU")})
    df["XOM"] = rng.normal(0, 0.015, n)
    df["JNJ"] = rng.normal(0, 0.01, n)
    return df


def test_clusters_detect_tech_concentration():
    r = corr_returns()
    cl = correlation_clusters(r, 0.7)
    assert len({cl[s] for s in ("NVDA", "AMD", "AVGO", "TSM", "MU")}) == 1
    assert cl["XOM"] != cl["NVDA"] and cl["JNJ"] != cl["XOM"]
    pos = {s: 1000.0 for s in ("NVDA", "AMD", "AVGO", "TSM", "MU")}
    rep = concentration_report(pos, r, {s: "Tech" for s in pos})
    assert rep["effective_bets"] < 1.6  # five tickers, ~one bet
    assert rep["largest_cluster_share"] == 1.0
    div = concentration_report({"NVDA": 1000.0, "XOM": 1000.0, "JNJ": 1000.0}, r, {})
    assert div["effective_bets"] > 2.3


def test_allocation_ignores_return_and_respects_health():
    r = pd.DataFrame({"hi_ret": rng.normal(0.01, 0.03, 300), "lo_vol": rng.normal(0.0005, 0.005, 300),
                      "sick": rng.normal(0.001, 0.01, 300)})
    w = allocate(r, health={"sick": 0.2}, cfg=AllocationConfig(max_weight=1.0))
    assert w["sick"] == 0 and w["lo_vol"] > w["hi_ret"]
    assert np.isclose(w.sum(), 1.0)
    capped = allocate(r, cfg=AllocationConfig(max_weight=0.4))
    assert capped.max() <= 0.4 + 1e-12 and capped.sum() < 1.0  # excess stays in cash
    assert allocate(r, health={k: 0 for k in r}).sum() == 0  # 100% cash allowed


def test_risk_parity_equalises_risk():
    r = corr_returns()[["NVDA", "XOM", "JNJ"]]
    w = allocate(r, cfg=AllocationConfig(max_weight=1.0))
    C = shrunk_covariance(r).to_numpy()
    rc = w.to_numpy() * (C @ w.to_numpy())
    assert rc.max() / rc.min() < 1.05


def test_calibrator():
    s = pd.Series(rng.uniform(0, 1, 3000))
    wins = pd.Series(rng.uniform(0, 1, 3000) < 0.3 + 0.4 * s)
    cal = ConfidenceCalibrator(n_bins=5, min_samples=30).fit(s, wins)
    assert cal.monotonic
    assert abs(cal.predict(0.95) - 0.68) < 0.06
    small = ConfidenceCalibrator(min_samples=1000).fit(s.iloc[:200], wins.iloc[:200])
    assert small.predict(0.5) is None  # no evidence -> no confidence number
    noise = ConfidenceCalibrator().fit(s, pd.Series(rng.uniform(0, 1, 3000) < 0.5))
    assert noise.table["win_rate"].between(0.43, 0.57).all()


def test_decay_detection():
    exp = {"win_rate": 0.55, "expectancy_r": 0.3, "mc_p5_drawdown": -0.15, "trades_per_day": 0.2, "equity": 10_000}
    good = pd.DataFrame({"r_multiple": rng.normal(0.3, 1, 80)})
    good["pnl"] = good["r_multiple"] * 100
    assert decay_check(good, exp)["recommendation"] in ("KEEP", "DEGRADED")
    bad = pd.DataFrame({"r_multiple": rng.normal(-0.4, 1, 80)})
    bad["pnl"] = bad["r_multiple"] * 100
    res = decay_check(bad, exp)
    assert res["recommendation"] in ("UNDER_REVIEW", "DISABLE") and res["health"] < 0.2
    assert decay_check(bad.iloc[:5], exp)["recommendation"] == "KEEP"  # too few trades for a verdict
