import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.backtest.benchmarks import momentum_baseline
from qsts.backtest.engine import BacktestConfig
from qsts.data.bars import Timeframe, to_canonical
from qsts.research.experiments import ExperimentTracker
from qsts.research.pipeline import ablation, run_pipeline, PipelineConfig
from qsts.research.scoring import deflated_sharpe, overfitting_risk, probabilistic_sharpe
from qsts.research.validation import (OOSAccessDenied, OOSVault, make_folds, monte_carlo_trades,
                                      neighborhood, parameter_robustness, walk_forward)
from qsts.strategy.definition import Condition, F, StopRule, StrategyDefinition, TakeProfitRule, V


@pytest.fixture(scope="module")
def data():
    return {s: to_canonical(synthetic_daily("2014-01-01", "2023-12-31", seed=i), Timeframe.D1)
            for i, s in enumerate(["AAA", "BBB", "CCC"])}


def mr_strategy(lo=30, n=14):
    return StrategyDefinition(
        name="rsi_mr", family="mean_reversion", hypothesis="Short-term oversold bounces",
        entry_long=(Condition(F("rsi", n="$n"), "<", V("$lo")),),
        exit_long=(Condition(F("rsi", n="$n"), ">", V(55.0)),),
        stop=StopRule("atr", 14, 2.5), take_profit=TakeProfitRule("r_multiple", 2.0), max_holding_bars=10,
        params={"lo": lo, "n": n})


def test_experiment_reproduce(sf, data):
    tr = ExperimentTracker(sf)
    rec = tr.run_backtest(mr_strategy(), data, BacktestConfig(), seed=7)
    assert tr.reproduce(rec.id, data)["reproduced"]
    tampered = dict(data)
    tampered["AAA"] = data["AAA"].copy()
    tampered["AAA"].iloc[100, 3] *= 1.01
    out = tr.reproduce(rec.id, tampered)
    assert not out["reproduced"] and "AAA" in out["reason"]
    assert tr.run_backtest(mr_strategy(), data, BacktestConfig(), seed=7).id == rec.id  # deterministic id


def test_folds_are_ordered_and_embargoed(data):
    idx = data["AAA"].index
    folds = make_folds(idx, 500, 100, 100, embargo=5)
    assert len(folds) > 5
    for f in folds:
        assert f.train[1] < f.validate[0] < f.validate[1] < f.test[0] <= f.test[1]
        assert idx.get_loc(f.validate[0]) - idx.get_loc(f.train[1]) == 6
    assert all(folds[i].test[0] > folds[i - 1].test[0] for i in range(1, len(folds)))


def test_walk_forward_runs_and_stitches(data):
    idx = data["AAA"].index
    folds = make_folds(idx, 500, 125, 125)[:3]
    wf = walk_forward(mr_strategy(), data, {"lo": [25, 30, 35]}, folds, BacktestConfig())
    assert wf.summary["n_folds"] == 3
    for r in [r for r in wf.folds if r["status"] == "OK"]:
        assert r["params"]["lo"] in (25, 30, 35)
    if len(wf.test_equity):
        assert wf.test_equity.index.min() >= folds[0].test[0]


def test_oos_vault(sf, data):
    vault = OOSVault(sf, "2022-01-01", max_evaluations=1)
    rv = vault.research_view(data)
    assert all(v.index.max() < pd.Timestamp("2022-01-01", tz="UTC") for v in rv.values())
    sd = mr_strategy()
    m = vault.evaluate(sd, data, BacktestConfig(), purpose="test")
    assert m["_equity"].index.min() >= pd.Timestamp("2022-01-01", tz="UTC")
    with pytest.raises(OOSAccessDenied):
        vault.evaluate(sd, data, BacktestConfig(), purpose="peek again")
    assert vault.accesses(sd.version_id) == 1
    vault.evaluate(sd.with_params(lo=31), data, BacktestConfig(), purpose="new version = new budget")


def test_neighborhood():
    assert neighborhood(35, rel=0.15) == list(range(29, 41)) or set(range(30, 41)) <= set(neighborhood(35, rel=0.15))
    assert len(neighborhood(2.0)) == 11


def test_parameter_robustness_runs(data):
    r = parameter_robustness(mr_strategy(), data, BacktestConfig(), data["AAA"].index[0], data["AAA"].index[-1],
                             space={"lo": [30, 32, 34, 36, 38, 40], "n": [12, 13, 15, 16]})
    assert set(r.neighbors) == {"lo", "n"} and len(r.neighbors["lo"]) == 5
    assert 0 <= r.stability <= 1 or np.isnan(r.stability)


def test_monte_carlo():
    rng = np.random.default_rng(0)
    trades = pd.DataFrame({"pnl": rng.normal(10, 100, 200)})
    mc = monte_carlo_trades(trades, 10_000, n_sims=500, seed=1)
    p = mc["max_drawdown"]
    assert p["p5"] <= p["p25"] <= p["p50"] <= p["p75"] <= p["p95"] <= 0
    sh = monte_carlo_trades(trades, 10_000, n_sims=50, seed=1, method="shuffle")
    assert np.isclose(sh["total_return"]["p5"], sh["total_return"]["p95"])  # permutation keeps final return
    assert monte_carlo_trades(trades.iloc[:10], 10_000)["insufficient_trades"]
    assert monte_carlo_trades(trades, 10_000, n_sims=200, seed=3) == monte_carlo_trades(trades, 10_000, n_sims=200, seed=3)


def test_psr_dsr():
    rng = np.random.default_rng(0)
    strong = pd.Series(rng.normal(0.002, 0.01, 1000))
    noise = pd.Series(rng.normal(0.0, 0.01, 1000))
    assert probabilistic_sharpe(strong) > 0.99
    assert deflated_sharpe(strong, 1) > deflated_sharpe(strong, 1000)
    assert deflated_sharpe(noise, 1000) < 0.5


def test_overfit_flags_perfect_and_tiny():
    risky = overfitting_risk(metrics={"n_trades": 8, "sharpe": 4.5, "profit_factor": 9.0, "win_rate": 0.9},
                             complexity={"n_params": 6, "n_rules": 6, "n_features": 5, "score": 17})
    calm = overfitting_risk(metrics={"n_trades": 400, "sharpe": 0.9, "profit_factor": 1.4, "win_rate": 0.5},
                            complexity={"n_params": 2, "n_rules": 2, "n_features": 1, "score": 5})
    assert risky["level"] == "HIGH" and risky["components"]["too_perfect"] == 1
    assert calm["score"] < risky["score"]
    assert "deflated_sharpe" in risky["missing"]


def test_pipeline_rejects_random_walk(sf, data):
    """On pure random-walk data there is no edge: the correct output is REJECTED (NO TRADE)."""
    vault = OOSVault(sf, "2022-01-01")
    rep = run_pipeline(mr_strategy(), data, vault, ExperimentTracker(sf), space={"lo": [25, 30, 35]},
                       pc=PipelineConfig(wf_train=504, wf_validate=126, wf_test=252, mc_sims=300))
    assert rep["steps"]["causality"] == "PASS"
    assert rep["decision"] == "REJECTED", rep["score"]["failed_gates"]
    assert "oos" in rep["steps"] and "ablation" in rep["steps"]


def test_ablation_keys(data):
    sd = StrategyDefinition(name="two", family="x", hypothesis="h",
                            entry_long=(Condition(F("rsi"), "<", V(40.0)), Condition(F("close"), ">", F("sma", n=200))),
                            stop=StopRule("atr", 14, 2.0))
    out = ablation(sd, data, BacktestConfig(), data["AAA"].index[0], data["AAA"].index[-1])
    assert "FULL" in out and len(out) == 3
