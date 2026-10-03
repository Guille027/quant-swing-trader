import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.backtest.benchmarks import buy_and_hold, momentum_baseline, trend_baseline
from qsts.backtest.engine import BacktestConfig, BacktestEngine, CostModel
from qsts.backtest.integrity import LookAheadError, check_strategy_causality
from qsts.backtest.metrics import compute_metrics, periodic_returns
from qsts.data.bars import Timeframe, nyse_sessions, to_canonical
from qsts.features.registry import REGISTRY, register
from qsts.strategy.definition import (Condition, F, StopRule, StrategyDefinition, TakeProfitRule, V,
                                      definition_from_dict)

ZERO = CostModel(spread_bps=0, slippage_bps=0, max_volume_participation=1.0)


def bars(rows, start="2023-01-03"):
    idx = nyse_sessions(start, "2023-12-31")[: len(rows)]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1e6
    return to_canonical(df, Timeframe.D1)


def strat(threshold=100.5, stop=0.05, tp=2.0, direction="long", **kw):
    cond = (Condition(F("close"), ">" if direction == "long" else "<", V(threshold)),)
    return StrategyDefinition(
        name="t", family="test", hypothesis="test", direction=direction,
        entry_long=cond if direction == "long" else (), entry_short=cond if direction == "short" else (),
        stop=StopRule("percent", mult=stop), take_profit=TakeProfitRule("r_multiple", tp) if tp else TakeProfitRule("none"),
        **kw)


def run(sd, df, **cfg):
    c = BacktestConfig(**({"costs": ZERO, "risk_per_trade": 0.01, "max_position_pct": 1.0} | cfg))
    return BacktestEngine(c).run(sd, {"X": df})


def test_entry_fills_next_open_not_signal_close():
    df = bars([[100, 100, 100, 100], [100, 101, 100, 101], [102, 103, 101.5, 102], [102, 102.5, 101.8, 102]])
    r = run(strat(tp=None), df)
    t = r.trades.iloc[0]
    assert t.entry_ts == df.index[2] and t.entry_price == 102  # signal at close of bar1 -> open of bar2


def test_stop_intrabar_and_gap():
    # signal bar1 close (101); entry bar2 open 100 -> stop 95 (5%)
    base = [[100, 100, 100, 100], [100, 101, 100, 101], [100, 100.5, 99, 100]]
    r = run(strat(tp=None), bars(base + [[99, 99.5, 94, 96]]))
    t = r.trades.iloc[0]
    assert t.exit_reason == "stop" and np.isclose(t.exit_price, 100 - 0.05 * 101)
    r = run(strat(tp=None), bars(base + [[90, 91, 89, 90]]))  # gap through stop
    t = r.trades.iloc[0]
    assert t.exit_reason == "stop_gap" and t.exit_price == 90


def test_stop_before_target_same_bar():
    base = [[100, 100, 100, 100], [100, 101, 100, 101], [100, 100.5, 99, 100]]
    r = run(strat(tp=2.0), bars(base + [[100, 111, 94, 100]]))  # target 110 and stop 95 both touched
    assert r.trades.iloc[0].exit_reason == "stop"


def test_target_hit():
    base = [[100, 100, 100, 100], [100, 101, 100, 101], [100, 100.5, 99, 100]]
    r = run(strat(tp=2.0), bars(base + [[101, 111, 100, 105]]))
    t = r.trades.iloc[0]
    assert t.exit_reason == "target" and np.isclose(t.exit_price, 100 + 2 * 0.05 * 101) and np.isclose(t.r_multiple, 2.0)


def test_risk_based_sizing():
    base = [[100, 100, 100, 100], [100, 101, 100, 101], [100, 100.5, 99, 100], [99, 99.5, 94, 96]]
    r = run(strat(tp=None), bars(base), initial_capital=10_000)
    t = r.trades.iloc[0]
    # risk 1% of 10k = 100 ; stop dist = 5% * 101 (signal close) = 5.05 -> qty 19.80
    assert np.isclose(t.qty, 100 / 5.05)
    assert np.isclose(t.pnl, -100, rtol=1e-6)


def test_costs_and_cash_conservation(daily):
    df = to_canonical(daily, Timeframe.D1)
    cfg = BacktestConfig(costs=CostModel(commission_per_share=0.005, commission_min=1.0, spread_bps=10, slippage_bps=5))
    r = BacktestEngine(cfg).run(trend_baseline(), {"X": df})
    assert len(r.trades) > 0
    assert np.isclose(r.equity["equity"].iloc[-1], cfg.initial_capital + r.trades["pnl"].sum(), rtol=1e-9)
    assert (r.trades["costs"] > 0).all()
    assert (r.trades["spread_slippage"] > 0).all()
    assert np.allclose(r.trades["costs"], r.trades["commission_borrow"] + r.trades["spread_slippage"])


def test_costs_reduce_pnl(daily):
    df = to_canonical(daily, Timeframe.D1)
    free = BacktestEngine(BacktestConfig(costs=ZERO)).run(momentum_baseline(), {"X": df})
    costly = BacktestEngine(BacktestConfig(costs=CostModel(spread_bps=20, slippage_bps=20))).run(momentum_baseline(), {"X": df})
    assert costly.equity["equity"].iloc[-1] < free.equity["equity"].iloc[-1]


def test_partial_fill_volume_cap():
    df = bars([[100, 100, 100, 100], [100, 101, 100, 101], [100, 100.5, 99, 100], [100, 100.5, 99, 100]])
    df["volume"] = 1000.0
    cfg = BacktestConfig(costs=CostModel(spread_bps=0, slippage_bps=0, max_volume_participation=0.01),
                         initial_capital=1e6, max_position_pct=1.0)
    r = BacktestEngine(cfg).run(strat(tp=None), {"X": df})
    assert r.trades.iloc[0].qty == 10.0
    assert any(x["reason"] == "partial fill" for x in r.rejected_orders)


def test_short_trade_pnl():
    df = bars([[100, 100, 100, 100], [100, 100, 99, 99], [100, 100.5, 99.5, 100], [97, 97, 89, 90]])
    r = run(strat(threshold=99.5, direction="short", tp=2.0), df)
    t = r.trades.iloc[0]
    # entry short at 100, stop dist 5% of 99 = 4.95, target 100 - 9.9 = 90.1 hit
    assert t.direction == "SHORT" and t.exit_reason == "target" and np.isclose(t.exit_price, 90.1)
    assert t.pnl > 0


def test_small_capital_no_fractional_rejected():
    df = bars([[1000, 1000, 1000, 1000], [1000, 1010, 1000, 1010], [1000, 1005, 990, 1000], [1000, 1005, 990, 1000]])
    sd = strat(threshold=1005, tp=None)
    r = run(sd, df, initial_capital=100, allow_fractional=False)
    assert r.trades.empty and r.rejected_orders
    r = run(sd, df, initial_capital=100, allow_fractional=True)
    assert len(r.trades) == 1 and r.trades.iloc[0].qty < 1


def test_no_lookahead_by_truncation(daily):
    """Trades fully closed before a cut must be identical whether or not later data exists."""
    df = to_canonical(daily, Timeframe.D1)
    eng = BacktestEngine(BacktestConfig())
    full = eng.run(momentum_baseline(), {"X": df}).trades
    cut = df.index[500]
    part = eng.run(momentum_baseline(), {"X": df[df.index <= cut]}).trades
    a = full[full.exit_ts < df.index[499]].reset_index(drop=True)
    b = part[part.exit_ts < df.index[499]].reset_index(drop=True)
    assert len(a) > 0
    pd.testing.assert_frame_equal(a, b)


def test_causality_checker_catches_cheating(daily):
    df = to_canonical(daily, Timeframe.D1)
    if "_peek" not in REGISTRY:
        register("_peek", "test")(lambda d: d["close"].shift(-1) / d["close"] - 1)
    honest = momentum_baseline()
    check_strategy_causality(honest, df)
    cheat = StrategyDefinition(name="cheat", family="test", hypothesis="peeks",
                               entry_long=(Condition(F("_peek"), ">", V(0.0)),), stop=StopRule("atr", 14, 2.0))
    with pytest.raises(LookAheadError):
        check_strategy_causality(cheat, df)


def test_definition_roundtrip_and_versioning():
    sd = strat()
    import json
    back = definition_from_dict(json.loads(json.dumps(sd.to_dict())))
    assert back.version_id == sd.version_id
    assert strat(threshold=101).version_id != sd.version_id


def test_param_placeholders():
    sd = StrategyDefinition(name="p", family="mr", hypothesis="h",
                            entry_long=(Condition(F("rsi", n="$n"), "<", V("$lo")),),
                            params={"n": 14, "lo": 30})
    sd.validate()
    assert sd.with_params(lo=35).version_id != sd.version_id
    with pytest.raises(KeyError):
        sd.with_params(zz=1)
    assert sd.complexity() == {"n_params": 2, "n_rules": 1, "n_features": 1, "score": 4}


def test_metrics_known_values():
    idx = pd.date_range("2020-01-01", periods=5, freq="D")
    eq = pd.DataFrame({"equity": [100, 110, 99, 120, 120.0], "gross_exposure": [0, 1, 1, 1, 0]}, index=idx)
    tr = pd.DataFrame({"pnl": [10, -11, 21.0], "r_multiple": [1, -1.1, 2.1], "bars_held": [1, 1, 1],
                       "qty": [1, 1, 1.0], "entry_price": [100, 110, 99.0], "exit_price": [110, 99, 120.0], "costs": [0, 0, 0.0]})
    m = compute_metrics(eq, tr)
    assert np.isclose(m["total_return"], 0.2)
    assert np.isclose(m["max_drawdown"], 99 / 110 - 1)
    assert m["n_trades"] == 3 and np.isclose(m["win_rate"], 2 / 3)
    assert np.isclose(m["profit_factor"], 31 / 11)
    assert m["longest_win_streak"] == 1 and m["longest_loss_streak"] == 1


def test_buy_and_hold_benchmark(daily):
    bh = buy_and_hold(daily["close"], BacktestConfig(costs=ZERO))
    assert np.isclose(bh["equity"].iloc[-1] / 10_000, daily["close"].iloc[-1] / daily["close"].iloc[0])
    yr = periodic_returns(bh["equity"], "YE")
    assert len(yr) == 3
