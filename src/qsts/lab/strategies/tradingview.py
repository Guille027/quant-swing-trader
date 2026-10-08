"""TradingView's built-in example strategies (the ones in the 'Strategies' list of every chart), with their default
settings. They are stop-and-reverse: always in the market, long or short. TradingView sends some of them as stop
orders at the indicator level; here every signal is confirmed at the close and filled at the next open."""
from __future__ import annotations

import numpy as np
import pandas as pd

from qsts.lab import ta
from qsts.lab.strategy import Strategy, register

SRC = "Estrategia de ejemplo incluida en TradingView ('{}'), con sus ajustes por defecto"


def _reverse(long_sig, short_sig, index) -> pd.DataFrame:
    return pd.DataFrame({"entry": np.where(long_sig, 1, np.where(short_sig, -1, 0))}, index=index)


@register
class TVMovingAverageCross(Strategy):
    key = "tv_ma_cross"
    name = "Cruce de medias 9/18 (TradingView)"
    source = SRC.format("MovingAvg2Line Cross")
    summary = "Largo cuando la media de 9 sesiones cruza por encima de la de 18; corto cuando la cruza hacia abajo."
    params = {"fast": 9, "slow": 18}
    param_grid = {"fast": [5, 9, 13], "slow": [14, 18, 26]}
    allow_short = True
    style = "semanas"

    def signals(self, bars, p):
        f, s = ta.sma(bars["close"], p["fast"]), ta.sma(bars["close"], p["slow"])
        return _reverse(ta.crossover(f, s), ta.crossunder(f, s), bars.index)


@register
class TVMACD(Strategy):
    key = "tv_macd"
    name = "MACD 12/26/9 (TradingView)"
    source = SRC.format("MACD Strategy")
    summary = "Largo cuando el MACD cruza por encima de su línea de señal; corto cuando la cruza hacia abajo."
    params = {"fast": 12, "slow": 26, "signal": 9}
    param_grid = {"fast": [8, 12, 16], "slow": [21, 26, 34], "signal": [7, 9, 12]}
    allow_short = True
    style = "semanas"

    def signals(self, bars, p):
        m = ta.macd(bars["close"], p["fast"], p["slow"], p["signal"])
        delta = m["macd"] - m["signal"]
        return _reverse(ta.crossover(delta, 0), ta.crossunder(delta, 0), bars.index)


@register
class TVRSI(Strategy):
    key = "tv_rsi"
    name = "RSI 14 sobreventa/sobrecompra (TradingView)"
    source = SRC.format("RSI Strategy")
    summary = ("Largo cuando el RSI de 14 sesiones sale de la sobreventa (cruza 30 hacia arriba); corto cuando sale de "
               "la sobrecompra (cruza 70 hacia abajo).")
    params = {"length": 14, "oversold": 30.0, "overbought": 70.0}
    param_grid = {"length": [10, 14, 20], "oversold": [25.0, 30.0, 35.0], "overbought": [65.0, 70.0, 75.0]}
    allow_short = True
    style = "semanas"
    notes = "TradingView usa órdenes stop en el nivel de la señal; aquí se confirma al cierre y se entra en la apertura."

    def signals(self, bars, p):
        r = ta.rsi(bars["close"], p["length"])
        return _reverse(ta.crossover(r, p["oversold"]), ta.crossunder(r, p["overbought"]), bars.index)


@register
class TVSuperTrend(Strategy):
    key = "tv_supertrend"
    name = "SuperTrend 10/3 (TradingView)"
    source = SRC.format("SuperTrend Strategy")
    summary = "Largo cuando el SuperTrend (ATR 10, factor 3) pasa a alcista; corto cuando pasa a bajista."
    params = {"atr_len": 10, "factor": 3.0}
    param_grid = {"atr_len": [7, 10, 14], "factor": [2.0, 3.0, 4.0]}
    allow_short = True
    style = "semanas"

    def signals(self, bars, p):
        d = ta.supertrend(bars, p["atr_len"], p["factor"])["direction"]
        prev = d.shift(1)
        return _reverse((d == 1) & (prev == -1), (d == -1) & (prev == 1), bars.index)


@register
class TVParabolicSAR(Strategy):
    key = "tv_psar"
    name = "Parabolic SAR (TradingView)"
    source = SRC.format("Parabolic SAR Strategy") + "; indicador de J. Welles Wilder (1978)"
    summary = ("Largo cuando el SAR parabólico pasa por debajo del precio; corto cuando pasa por encima "
               "(siempre dentro del mercado).")
    params = {"start": 0.02, "increment": 0.02, "maximum": 0.2}
    param_grid = {"start": [0.01, 0.02, 0.03], "maximum": [0.1, 0.2, 0.3]}
    allow_short = True
    style = "semanas"
    notes = "TradingView entra con órdenes stop en el nivel del SAR; aquí al confirmarse el giro al cierre."

    def signals(self, bars, p):
        d = ta.psar(bars, p["start"], p["increment"], p["maximum"])["direction"]
        prev = d.shift(1)
        return _reverse((d == 1) & (prev == -1), (d == -1) & (prev == 1), bars.index)


@register
class TVBollinger(Strategy):
    key = "tv_bollinger"
    name = "Bandas de Bollinger 20/2 (TradingView)"
    source = SRC.format("Bollinger Bands Strategy") + "; bandas de John Bollinger"
    summary = ("Largo cuando el precio vuelve a entrar en las bandas desde abajo (cruza la banda inferior hacia arriba); "
               "corto cuando vuelve a entrar desde arriba (cruza la banda superior hacia abajo).")
    params = {"length": 20, "mult": 2.0}
    param_grid = {"length": [15, 20, 30], "mult": [1.5, 2.0, 2.5]}
    allow_short = True
    style = "semanas"
    notes = "TradingView usa órdenes stop en las bandas; aquí la señal se confirma al cierre."

    def signals(self, bars, p):
        bb = ta.bollinger(bars["close"], p["length"], p["mult"])
        c = bars["close"]
        return _reverse(ta.crossover(c, bb["lower"]), ta.crossunder(c, bb["upper"]), bars.index)
