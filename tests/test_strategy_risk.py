import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.backtest.benchmarks import trend_baseline
from qsts.core.kill_switch import KillSwitch
from qsts.data.bars import Timeframe, to_canonical
from qsts.risk.engine import MICRO_LIVE, OpenPosition, PortfolioState, RiskEngine, SizeTier, TradeIntent
from qsts.strategy.lifecycle import LifecycleError, Status, StrategyRegistry
from qsts.strategy.regime import RegimeConfig, classify


# ------------------------------------------------------------------ regime
def test_regime_labels_and_causality():
    up = to_canonical(synthetic_daily(drift=0.002, vol=0.01, seed=1), Timeframe.D1)
    dn = to_canonical(synthetic_daily(drift=-0.002, vol=0.01, seed=1), Timeframe.D1)
    assert (classify(up)["trend"].dropna() == "BULL").mean() > 0.8
    assert (classify(dn)["trend"].dropna() == "BEAR").mean() > 0.8
    full = classify(up)
    part = classify(up.iloc[:400])
    pd.testing.assert_frame_equal(part, full.iloc[:400])


def test_regime_high_vol_detected():
    calm = synthetic_daily("2020-01-01", "2021-12-31", vol=0.005, seed=2)
    wild = synthetic_daily("2022-01-01", "2022-06-30", vol=0.04, seed=3, s0=calm["close"].iloc[-1])
    df = to_canonical(pd.concat([calm, wild]), Timeframe.D1)
    v = classify(df)["volatility"]
    assert (v.loc["2022-03":"2022-06"] == "HIGH_VOLATILITY").mean() > 0.7


# ------------------------------------------------------------------ lifecycle
def test_lifecycle_rules(sf):
    reg = StrategyRegistry(sf)
    v1 = reg.register("trend1", trend_baseline(), origin="ai")
    assert reg.register("trend1", trend_baseline(), origin="ai") == v1  # idempotent version
    with pytest.raises(LifecycleError):
        reg.transition("trend1", Status.CANDIDATE, reason="skip", actor="ai")
    with pytest.raises(LifecycleError):
        reg.transition("trend1", Status.BACKTESTED, reason="no evidence", actor="ai")
    reg.transition("trend1", Status.BACKTESTED, reason="bt", actor="system", evidence={"backtest_experiment_id": "e1"})
    reg.transition("trend1", Status.VALIDATING, reason="v", actor="system")
    with pytest.raises(LifecycleError):
        reg.transition("trend1", Status.CANDIDATE, reason="partial", actor="system",
                       evidence={"walk_forward_experiment_id": "w"})
    reg.transition("trend1", Status.CANDIDATE, reason="ok", actor="system", evidence={
        "walk_forward_experiment_id": "w", "robustness_passed": True, "oos_experiment_id": "o",
        "monte_carlo_experiment_id": "m"})
    reg.transition("trend1", Status.PAPER, reason="p", actor="system")
    with pytest.raises(LifecycleError):
        reg.transition("trend1", Status.APPROVED, reason="ai approves itself", actor="ai",
                       evidence={"paper_trading_report_id": "r"})
    reg.transition("trend1", Status.APPROVED, reason="human", actor="user", evidence={"paper_trading_report_id": "r"})
    reg.transition("trend1", Status.ACTIVE, reason="go", actor="user")
    reg.transition("trend1", Status.DEGRADED, reason="decay", actor="system")
    reg.transition("trend1", Status.DISABLED, reason="limit", actor="system")
    h = reg.history("trend1")
    assert h[0] == (None, "RESEARCH", "ai") and h[-1][1] == "DISABLED" and len(h) == 9


# ------------------------------------------------------------------ risk
def state(**kw):
    d = dict(equity=10_000, cash=10_000, peak_equity=10_000, day_start_equity=10_000, week_start_equity=10_000)
    return PortfolioState(**(d | kw))


def intent(**kw):
    return TradeIntent(**(dict(symbol="AAA", direction=1, entry=100.0, stop=95.0, sector="Tech") | kw))


def test_basic_sizing():
    d = RiskEngine().evaluate(intent(), state())
    assert d.approved and np.isclose(d.risk_amount, 100) and np.isclose(d.qty, 20)
    assert d.tier is SizeTier.NORMAL


def test_kill_switch_blocks(tmp_path):
    ks = KillSwitch(tmp_path)
    ks.engage("test")
    d = RiskEngine(kill_switch=ks).evaluate(intent(), state())
    assert not d.approved and "kill switch engaged" in d.reasons


def test_multiple_reasons_reported():
    d = RiskEngine().evaluate(intent(data_valid=False, broker_available=False, spread_bps=100), state())
    assert not d.approved and len(d.reasons) == 3


def test_short_requires_confirmed_availability():
    e = RiskEngine()
    assert not e.evaluate(intent(direction=-1, stop=105), state()).approved
    assert e.evaluate(intent(direction=-1, stop=105, shortable=True), state()).approved
    assert not RiskEngine(MICRO_LIVE).evaluate(intent(direction=-1, stop=105, shortable=True), state()).approved


def test_stop_wrong_side():
    assert not RiskEngine().evaluate(intent(stop=101), state()).approved


def test_loss_limits_and_drawdown_scaling():
    e = RiskEngine()
    assert not e.evaluate(intent(), state(equity=9_600, day_start_equity=10_000, peak_equity=10_000)).approved
    d = e.evaluate(intent(), state(equity=8_750, cash=8_750, peak_equity=10_000, day_start_equity=8_750, week_start_equity=8_750))
    assert d.approved and np.isclose(e.drawdown_multiplier(-0.125), 0.5)
    assert np.isclose(d.risk_amount, 0.01 * 8_750 * 0.5)
    assert not e.evaluate(intent(), state(equity=7_900, cash=7_900, peak_equity=10_000, day_start_equity=7_900,
                                           week_start_equity=7_900)).approved


def test_sector_and_correlation_caps():
    pos = [OpenPosition(s, 1, 20, 100, 95, "Tech") for s in ("NVDA", "AMD")]
    st = state(cash=6_000, positions=pos)
    d = RiskEngine().evaluate(intent(symbol="AVGO"), st)
    # sector cap 40% of 10k = 4000; 4000 already used -> nothing left
    assert not d.approved
    rng = np.random.default_rng(0)
    common = rng.normal(size=300)
    rets = pd.DataFrame({s: common + rng.normal(scale=0.2, size=300) for s in ("NVDA", "AMD", "AVGO")})
    d = RiskEngine().with_limits(max_sector_exposure=1.0, risk_per_trade=0.02, max_position_pct=1.0).evaluate(intent(symbol="AVGO"), st, rets)
    # correlated risk cap 3% = 300; NVDA+AMD already carry 2*20*5 = 200 -> only 100 left (1R=5 -> 20 sh... capped)
    assert d.approved and any("correlated" in a for a in d.adjustments) and np.isclose(d.risk_amount, 100)


def test_micro_live_100_eur():
    e = RiskEngine(MICRO_LIVE)
    st = state(equity=100, cash=100, peak_equity=100, day_start_equity=100, week_start_equity=100)
    d = e.evaluate(intent(entry=200, stop=190), st)
    assert d.approved and d.risk_amount <= 1.0 + 1e-9 and d.notional <= 50 + 1e-9
    assert not e.with_limits(allow_fractional=False).evaluate(intent(entry=200, stop=190), st).approved


def test_calibrated_confidence_tiers():
    e = RiskEngine()
    assert e.evaluate(intent(calibrated_confidence=0.7), state()).tier is SizeTier.HIGH_CONVICTION
    assert e.evaluate(intent(calibrated_confidence=0.4), state()).tier is SizeTier.SMALL
    assert e.evaluate(intent(), state()).tier is SizeTier.NORMAL  # no calibration -> never high conviction
