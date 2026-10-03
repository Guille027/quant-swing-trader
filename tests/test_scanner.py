import numpy as np
import pandas as pd

from conftest import synthetic_daily
from qsts.app.scanner import MarketScanner, StrategySlot, render_report
from qsts.risk.engine import PortfolioState, RiskEngine
from qsts.strategy.definition import Condition, F, StopRule, StrategyDefinition, V

DATA = {s: synthetic_daily("2020-01-01", "2022-12-30", seed=i) for i, s in enumerate(["SPY", "AAA", "BBB", "CCC"])}
DATA["BAD"] = DATA["AAA"].iloc[::3]  # many missing sessions -> invalid


def always_long():
    return StrategyDefinition(name="always", family="test", hypothesis="test",
                              entry_long=(Condition(F("close"), ">", V(0.0)),), stop=StopRule("atr", 14, 2.0))


def pstate():
    return PortfolioState(10_000, 10_000, 10_000, 10_000, 10_000)


def scanner(status="PAPER"):
    return MarketScanner(lambda s: DATA[s], [StrategySlot("always_v1", always_long(), status)], RiskEngine())


def test_scan_counts_and_invalid():
    rep = scanner().scan(["AAA", "BBB", "CCC", "BAD", "MISSING"], "2022-06-01 21:00", pstate())
    assert rep.assets_scanned == 5 and rep.valid_assets == 3
    assert set(rep.invalid) == {"BAD", "MISSING"}
    assert rep.final_signals == 3 and all(s.confidence is None for s in rep.signals)
    assert rep.regime.get("trend") is not None


def test_scan_uses_only_available_bars():
    # at 15:00 UTC on 2022-06-01 the session has not closed: last usable bar is 2022-05-31
    rep = scanner().scan(["AAA"], "2022-06-01 15:00", pstate())
    assert rep.signals[0].snapshot["bar_ts"].startswith("2022-05-31")
    rep2 = scanner().scan(["AAA"], "2022-06-01 21:00", pstate())
    assert rep2.signals[0].snapshot["bar_ts"].startswith("2022-06-01")


def test_research_strategies_never_trade():
    rep = scanner(status="CANDIDATE").scan(["AAA"], "2022-06-01 21:00", pstate())
    assert rep.final_signals == 0 and rep.potential_setups == 0


def test_render():
    rep = scanner().scan(["AAA", "BAD"], "2022-06-01 21:00", pstate())
    txt = render_report(rep, pstate(), {"active": 0, "candidate": 1}, {"Data": "OK", "Broker": "PAPER"})
    assert "Valid assets: 1" in txt and "not calibrated" in txt and "Broker: PAPER" in txt
