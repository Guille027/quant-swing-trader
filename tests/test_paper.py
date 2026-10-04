"""Paper trading = the backtest engine run forward. SYNTHETIC random-walk data only."""
import numpy as np
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


def test_paper_uses_the_rules_the_strategy_was_validated_with(sf):
    from qsts.db import models as m
    reg = StrategyRegistry(sf)
    reg.register("s2", SD)
    rec = ExperimentTracker(sf).run_backtest(SD, {k: DATA[k] for k in ("AAA", "BBB")},
                                             BacktestConfig(earnings_blackout_days=3, exit_before_earnings=True),
                                             strategy_id="s2")
    reg.transition("s2", Status.BACKTESTED, reason="t", actor="system", evidence={"backtest_experiment_id": rec.id})
    reg.transition("s2", Status.VALIDATING, reason="t", actor="system")
    reg.transition("s2", Status.CANDIDATE, reason="t", actor="user",
                   evidence={"walk_forward_experiment_id": "x", "robustness_passed": True, "oos_experiment_id": "y",
                             "monte_carlo_experiment_id": "z"})
    pt = PaperTrading(sf, load)
    sid = pt.start("s2", 7_000, asof="2022-06-01 21:00")
    with sf() as s:
        cfg = s.get(m.PaperSession, sid).config
    assert cfg["earnings_blackout_days"] == 3 and cfg["exit_before_earnings"] is True and cfg["initial_capital"] == 7000
    assert pt.view(until="2022-06-10")["earnings_rule"] == {"blackout_days": 3, "exit_before": True}


def _fx():
    """SYNTHETIC euro/dollar rate: 1.10 until July 2022, then 1.20."""
    idx = DATA["AAA"].index
    return pd.Series(np.where(idx < pd.Timestamp("2022-07-01", tz="UTC"), 1.10, 1.20), index=idx)


def test_eur_account_sizes_in_euros(sf):
    candidate(sf)
    with pytest.raises(PaperError):  # no exchange rate stored yet
        PaperTrading(sf, load).start("s1", 2363, currency="EUR", asof="2022-06-01 21:00")
    pt = PaperTrading(sf, load, fx=_fx)
    with pytest.raises(PaperError):
        pt.start("s1", 2363, currency="GBP", asof="2022-06-01 21:00")
    pt.start("s1", 2363, currency="EUR", asof="2022-06-01 21:00")
    v0 = pt.view(until="2022-06-01 21:00")
    assert v0["currency"] == "EUR" and v0["equity"] == pytest.approx(2363) and v0["fx"] == pytest.approx(1.10)
    for o in v0["orders"]:
        assert o["approx_value"] == pytest.approx(o["approx_value_usd"] / 1.10)
    assert sum(o["approx_value"] for o in v0["orders"] if o["action"] == "COMPRAR") <= 2363 + 1e-6
    v1 = pt.view(until="2022-09-30")
    # the engine runs in dollars (2363 * 1.10); euros at each day's rate
    assert v1["equity"] == pytest.approx(2363 * 1.10 * (1 + v1["return_usd"]) / 1.20)
    assert v1["curve"][0]["value"] == pytest.approx(2363) and pt.summary()["currency"] == "EUR"
    assert pt.journal()[0]["equity"] == pytest.approx(2363)


def test_daily_telegram_report_once_per_close(sf):
    from types import SimpleNamespace
    from qsts.app.daily import DailyReporter
    candidate(sf)
    pt = PaperTrading(sf, load)
    sent = []
    tg = SimpleNamespace(send=lambda text: sent.append(text) or 1)
    runner = SimpleNamespace(state=SimpleNamespace(running=False), calls=[])
    runner.start = lambda kind, symbols, incremental: runner.calls.append(symbols) or True
    last = {"bar": pd.Timestamp("2022-12-29", tz="UTC")}
    rep = DailyReporter(sf, lambda: pt, lambda: tg, lambda: runner, lambda s: last["bar"], delay_min=45)
    now = pd.Timestamp("2022-12-30 23:00", tz="UTC")  # after the NYSE close of 2022-12-30 + 45 min
    assert rep.tick(now).startswith("sin simulación") and not sent
    pt.start("s1", 10_000, asof="2022-06-01 21:00")
    assert DailyReporter(sf, lambda: pt, lambda: None).tick(now) == "Telegram no configurado"
    assert rep.tick(now).startswith("descargando") and runner.calls and "SPY" in runner.calls[0] and not sent
    assert rep.tick(now).startswith("esperando")  # retried only every few minutes
    last["bar"] = pd.Timestamp("2022-12-30", tz="UTC")
    assert rep.tick(now) == "aviso del 2022-12-30 enviado" and sent
    assert any("QSTS · cierre del viernes 30 dic" in t for t in sent)
    n = len(sent)
    assert rep.tick(now) == "aviso del 2022-12-30 ya enviado" and len(sent) == n  # never twice
    assert rep.tick(pd.Timestamp("2022-12-31 12:00", tz="UTC")) == "aviso del 2022-12-30 ya enviado"  # weekend
