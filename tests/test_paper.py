"""Paper trading = the backtest engine run forward. SYNTHETIC random-walk data only."""
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.backtest.engine import BacktestConfig, BacktestEngine
from qsts.data.bars import Timeframe, to_canonical
from qsts.execution.paper import PaperError, PaperTrading
from qsts.research.experiments import ExperimentTracker
from qsts.strategy.definition import Condition, F, StopRule, StrategyDefinition, TakeProfitRule, V
from qsts.strategy.lifecycle import Status, StrategyRegistry

DATA = {s: to_canonical(synthetic_daily("2018-01-01", "2022-12-30", seed=60 + i), Timeframe.D1)
        for i, s in enumerate(["SPY", "AAA", "BBB", "CCC"])}
SD = StrategyDefinition(name="dip", family="t", hypothesis="h",
                        entry_long=(Condition(F("rsi", n=14), "<", V(45.0)),),
                        exit_long=(Condition(F("rsi", n=14), ">", V(55.0)),),
                        stop=StopRule("atr", 14, 2.0), take_profit=TakeProfitRule("none"), max_holding_bars=5)


def load(sym):
    return DATA[sym]


def candidate(sf):
    reg = StrategyRegistry(sf)
    reg.register("s1", SD)
    rec = ExperimentTracker(sf).run_backtest(SD, {k: DATA[k] for k in ("AAA", "BBB", "CCC")}, BacktestConfig(),
                                             strategy_id="s1")
    reg.transition("s1", Status.BACKTESTED, reason="t", actor="system", evidence={"backtest_experiment_id": rec.id})
    reg.transition("s1", Status.VALIDATING, reason="t", actor="system")
    reg.transition("s1", Status.CANDIDATE, reason="t", actor="user",
                   evidence={"walk_forward_experiment_id": "x", "robustness_passed": True, "oos_experiment_id": "y",
                             "monte_carlo_experiment_id": "z"})
    return reg


def test_engine_can_keep_positions_open():
    d = {k: DATA[k] for k in ("AAA", "BBB")}
    closed = BacktestEngine().run(SD, d, start=pd.Timestamp("2022-01-03", tz="UTC"))
    open_ = BacktestEngine().run(SD, d, start=pd.Timestamp("2022-01-03", tz="UTC"), close_at_end=False)
    assert closed.open_positions == [] and closed.pending_orders == []
    n_end = int((closed.trades["exit_reason"] == "end_of_data").sum())
    assert len(open_.trades) == len(closed.trades) - n_end and len(open_.open_positions) == n_end


def test_paper_session_runs_forward_only(sf):
    reg = candidate(sf)
    pt = PaperTrading(sf, load)
    assert [c["strategy_id"] for c in pt.view()["candidates"]] == ["s1"]
    with pytest.raises(PaperError):  # prices far behind "today": no back-dated paper results
        pt.start("s1", asof="2023-03-01 22:00")
    pt.start("s1", 10_000, asof="2022-06-01 21:00")
    assert reg.status("s1") is Status.PAPER
    with pytest.raises(PaperError):
        pt.start("s1")  # one session at a time
    v0 = pt.view(until="2022-06-01 21:00")
    assert v0["equity"] == pytest.approx(10_000) and v0["days"] == 0 and v0["as_of"] == "2022-06-01"
    buys = {o["symbol"] for o in v0["orders"] if o["action"] == "COMPRAR"}
    v1 = pt.view(until="2022-09-30")
    entered_next_open = {p["symbol"] for p in v1["positions"] if p["entry_ts"] == "2022-06-02"} | \
                        {t["symbol"] for t in v1["closed"] if t["entry"] == "2022-06-02"}
    assert entered_next_open == buys  # what it said in the evening is what it did at the next open
    v2 = pt.view(until="2022-12-30")
    assert [t for t in v2["closed"] if t["exit"] <= "2022-09-30"] == v1["closed"]  # the past does not change
    assert v2["revisions"]["days_changed"] == 0 and v2["days"] > v1["days"]
    j = pt.journal()
    assert j[0]["day"] == "2022-06-01" and j[0]["orders"] is not None and j[-1]["day"] == "2022-12-30"
    pt.stop("test")
    assert reg.status("s1") is Status.UNDER_REVIEW and pt.view()["active"] is False


def test_only_candidates_can_be_simulated(sf):
    reg = StrategyRegistry(sf)
    reg.register("r1", SD)
    with pytest.raises(PaperError):
        PaperTrading(sf, load).start("r1", asof="2022-06-01 21:00")
