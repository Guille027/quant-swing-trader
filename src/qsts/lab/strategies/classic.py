"""Classic published strategies, as reference points (well known, simple, documented by their authors). They are
here so every new strategy has something honest to be compared with."""
from __future__ import annotations

import numpy as np
import pandas as pd

from qsts.lab import ta
from qsts.lab.strategy import Strategy, register


@register
class ConnorsRSI2(Strategy):
    key = "connors_rsi2"
    name = "RSI(2) de Larry Connors"
    source = "Larry Connors y Cesar Alvarez, 'Short Term Trading Strategies That Work' (2008)"
    summary = ("Compra cuando el precio está por encima de su media de 200 días y el RSI de 2 días baja de 10 "
               "(un retroceso fuerte dentro de una tendencia alcista). Vende cuando el cierre supera la media de 5 días.")
    default_symbols = ("SPY", "QQQ")
    params = {"rsi_len": 2, "rsi_buy": 10.0, "trend_len": 200, "exit_len": 5}
    param_grid = {"rsi_buy": [5.0, 10.0, 15.0], "exit_len": [3, 5, 10], "trend_len": [150, 200, 250]}
    style = "pocos días"
    notes = "Connors compra al cierre del día de la señal; aquí en la apertura siguiente (la señal solo se conoce al cierre)."

    def signals(self, bars, p):
        c = bars["close"]
        r = ta.rsi(c, p["rsi_len"])
        entry = (c > ta.sma(c, p["trend_len"])) & (r < p["rsi_buy"])
        return pd.DataFrame({"entry": entry.astype(int), "exit": c > ta.sma(c, p["exit_len"])}, index=bars.index)


@register
class GoldenCross(Strategy):
    key = "golden_cross"
    name = "Cruce dorado 50/200"
    source = "Regla clásica de análisis técnico (cruce de las medias de 50 y 200 sesiones)"
    summary = "Compra cuando la media de 50 días cruza por encima de la de 200; vende cuando la cruza hacia abajo."
    default_symbols = ("SPY", "QQQ")
    params = {"fast": 50, "slow": 200}
    param_grid = {"fast": [30, 50, 70], "slow": [150, 200, 250]}
    style = "meses"

    def signals(self, bars, p):
        c = bars["close"]
        f, s = ta.sma(c, p["fast"]), ta.sma(c, p["slow"])
        return pd.DataFrame({"entry": ta.crossover(f, s).astype(int), "exit": ta.crossunder(f, s)}, index=bars.index)


@register
class TurtleBreakout(Strategy):
    key = "turtle_20_10"
    name = "Tortugas: ruptura de 20 días"
    source = "Sistema 1 de las Tortugas de Richard Dennis (según Curtis Faith, 'Way of the Turtle', 2007)"
    summary = ("Compra si el cierre supera el máximo de los 20 días anteriores y vende en corto si baja del mínimo. "
               "Sale con la ruptura contraria de 10 días o con un stop a 2 ATR (de 20 días) del precio de entrada.")
    default_symbols = ("SPY", "QQQ", "GLD")
    params = {"entry_len": 20, "exit_len": 10, "atr_len": 20, "stop_atr": 2.0}
    param_grid = {"entry_len": [15, 20, 30], "exit_len": [7, 10, 15], "stop_atr": [1.5, 2.0, 3.0]}
    allow_short = True
    style = "semanas"
    notes = ("Las Tortugas entraban con una orden stop en el momento de la ruptura; aquí la ruptura se confirma al "
             "cierre y se entra en la apertura siguiente. Sin la regla de saltarse la señal tras una ganadora.")

    def signals(self, bars, p):
        c = bars["close"]
        up = ta.donchian(bars, p["entry_len"])
        ex = ta.donchian(bars, p["exit_len"])
        n = ta.atr(bars, p["atr_len"])
        entry = np.where(c > up["upper"], 1, np.where(c < up["lower"], -1, 0))
        long_exit, short_exit = c < ex["lower"], c > ex["upper"]
        # the stop is 2 ATR from the entry price: as a fraction of today's close (the entry is tomorrow's open)
        return pd.DataFrame({"entry": entry, "exit_long": long_exit, "exit_short": short_exit,
                             "stop_pct": p["stop_atr"] * n / c}, index=bars.index)


@register
class TurtleSystem2(TurtleBreakout):
    key = "turtle_55_20"
    name = "Tortugas: ruptura de 55 días"
    source = "Sistema 2 de las Tortugas de Richard Dennis (según Curtis Faith, 'Way of the Turtle', 2007)"
    summary = ("Compra si el cierre supera el máximo de los 55 días anteriores y vende en corto si baja del mínimo. "
               "Sale con la ruptura contraria de 20 días o con un stop a 2 ATR (de 20 días) del precio de entrada.")
    default_symbols = ("SPY", "GLD")
    params = {"entry_len": 55, "exit_len": 20, "atr_len": 20, "stop_atr": 2.0}
    param_grid = {"entry_len": [40, 55, 70], "exit_len": [15, 20, 30], "stop_atr": [1.5, 2.0, 3.0]}
    style = "meses"
