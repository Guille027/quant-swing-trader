"""Short-term mean reversion from Larry Connors and Cesar Alvarez, 'Short Term Trading Strategies That Work' (2008).
The book buys at the close of the signal day; here the order is filled at the next open (the signal is only known at
the close), as in TradingView's default."""
from __future__ import annotations

import pandas as pd

from qsts.lab import ta
from qsts.lab.strategy import Strategy, register


@register
class Double7s(Strategy):
    key = "double_7s"
    name = "Double 7's de Connors"
    source = "Larry Connors y Cesar Alvarez, 'Short Term Trading Strategies That Work' (2008), capítulo 'Double 7's'"
    summary = ("Solo si el precio está por encima de su media de 200 días: compra cuando el cierre marca el mínimo de "
               "los últimos 7 días y vende cuando marca el máximo de los últimos 7 días. Sin stop.")
    default_symbols = ("SPY", "QQQ")
    params = {"trend_len": 200, "days": 7}
    param_grid = {"days": [5, 7, 10], "trend_len": [150, 200, 250]}
    style = "pocos días"
    notes = "El libro compra al cierre; aquí en la apertura siguiente."

    def signals(self, bars, p):
        c = bars["close"]
        entry = (c > ta.sma(c, p["trend_len"])) & (c <= ta.lowest(c, p["days"]))
        return pd.DataFrame({"entry": entry.astype(int), "exit": c >= ta.highest(c, p["days"])}, index=bars.index)


@register
class CumulativeRSI(Strategy):
    key = "cumulative_rsi"
    name = "RSI acumulado de Connors"
    source = "Larry Connors y Cesar Alvarez, 'Short Term Trading Strategies That Work' (2008), capítulo 'Cumulative RSI'"
    summary = ("Solo si el precio está por encima de su media de 200 días: compra cuando la suma del RSI(2) de los "
               "dos últimos días baja de 35. Vende cuando el RSI(2) supera 65.")
    default_symbols = ("SPY", "QQQ")
    params = {"rsi_len": 2, "days": 2, "buy_below": 35.0, "sell_above": 65.0, "trend_len": 200}
    param_grid = {"buy_below": [25.0, 35.0, 45.0], "sell_above": [55.0, 65.0, 75.0], "days": [2, 3]}
    style = "pocos días"
    notes = "El libro compra al cierre; aquí en la apertura siguiente."

    def _cum(self, c, p):
        return ta.rsi(c, p["rsi_len"]).rolling(p["days"], min_periods=p["days"]).sum()

    def signals(self, bars, p):
        c = bars["close"]
        entry = (c > ta.sma(c, p["trend_len"])) & (self._cum(c, p) < p["buy_below"])
        return pd.DataFrame({"entry": entry.astype(int), "exit": ta.rsi(c, p["rsi_len"]) > p["sell_above"]},
                            index=bars.index)
