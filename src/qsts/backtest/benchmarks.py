"""Baselines every strategy must be compared against."""
from __future__ import annotations

import pandas as pd

from qsts.backtest.engine import BacktestConfig
from qsts.strategy.definition import Condition, F, StopRule, StrategyDefinition, TakeProfitRule, V


def buy_and_hold(close: pd.Series, cfg: BacktestConfig = BacktestConfig()) -> pd.DataFrame:
    """Buy at the first close (paying entry costs), hold to the end."""
    px = cfg.costs.fill_price(close.iloc[0], +1)
    qty = cfg.initial_capital / (px * (1 + cfg.costs.commission_pct) + cfg.costs.commission_per_share)
    qty = min(qty, (cfg.initial_capital - cfg.costs.commission(qty, px)) / px)
    cash = cfg.initial_capital - qty * px - cfg.costs.commission(qty, px)
    eq = cash + qty * close
    return pd.DataFrame({"equity": eq, "gross_exposure": qty * close / eq})


def risk_free(index: pd.DatetimeIndex, annual_rate: float, capital: float, bars_per_year: int = 252) -> pd.DataFrame:
    g = (1 + annual_rate) ** (1 / bars_per_year)
    eq = pd.Series(capital * g ** range(len(index)), index=index)
    return pd.DataFrame({"equity": eq, "gross_exposure": 0.0})


def momentum_baseline() -> StrategyDefinition:
    """Long when 126-bar return is positive; exit when it turns negative."""
    return StrategyDefinition(
        name="baseline_momentum", family="momentum", hypothesis="Positive 6-month momentum persists",
        entry_long=(Condition(F("roc", n=126), ">", V(0.0)),),
        exit_long=(Condition(F("roc", n=126), "<", V(0.0)),),
        stop=StopRule("atr", 14, 4.0), take_profit=TakeProfitRule("none"),
    )


def trend_baseline() -> StrategyDefinition:
    """Long when close > SMA200; exit below."""
    return StrategyDefinition(
        name="baseline_trend", family="trend_following", hypothesis="Price above long-term average trends up",
        entry_long=(Condition(F("close"), ">", F("sma", n=200)),),
        exit_long=(Condition(F("close"), "<", F("sma", n=200)),),
        stop=StopRule("atr", 14, 4.0), take_profit=TakeProfitRule("none"),
    )
