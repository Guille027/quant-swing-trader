"""Simple statistical edges popularised by Quantified Strategies (quantifiedstrategies.com) and gap-fill traders.
Their tests buy and sell at the close; here signals are known at the close and orders filled at the next open (or,
for the intraday ones, from the next open to that day's close)."""
from __future__ import annotations

import pandas as pd

from qsts.lab import ta
from qsts.lab.strategy import Strategy, register


@register
class IBSReversion(Strategy):
    key = "ibs_reversion"
    name = "IBS: cierre en la parte baja del día"
    source = "Quantified Strategies, 'Internal Bar Strength (IBS) trading strategy' (reglas publicadas para SPY)"
    summary = ("El IBS mide dónde cierra el día dentro de su rango (0 = en el mínimo, 1 = en el máximo). Compra cuando "
               "el IBS baja de 0,2 y vende cuando sube de 0,8.")
    default_symbols = ("SPY", "QQQ")
    params = {"buy_below": 0.2, "sell_above": 0.8}
    param_grid = {"buy_below": [0.1, 0.2, 0.3], "sell_above": [0.7, 0.8, 0.9]}
    style = "pocos días"
    notes = "Quantified Strategies compra y vende al cierre; aquí en la apertura siguiente a cada señal."

    def signals(self, bars, p):
        i = ta.ibs(bars)
        return pd.DataFrame({"entry": (i < p["buy_below"]).astype(int), "exit": i > p["sell_above"]}, index=bars.index)


@register
class TurnaroundTuesday(Strategy):
    key = "turnaround_tuesday"
    name = "Turnaround Tuesday (intradía)"
    source = "Quantified Strategies, 'Turnaround Tuesday strategy' (efecto descrito desde 2012)"
    summary = ("Si el lunes cierra más bajo que el viernes, compra en la apertura del martes y vende en el cierre del "
               "martes (operación de un solo día).")
    default_symbols = ("SPY", "QQQ")
    params = {"min_drop": 0.0}
    param_grid = {"min_drop": [0.0, 0.005, 0.01]}
    day_trade = True
    style = "intradía"
    notes = ("El original compra al cierre del lunes y vende al cierre del martes. Como la caída del lunes solo se "
             "conoce a su cierre, aquí se opera solo la parte del martes (de la apertura al cierre).")

    def signals(self, bars, p):
        c = bars["close"]
        monday = pd.Series(bars.index.dayofweek == 0, index=bars.index)
        down = c < c.shift(1) * (1 - p["min_drop"])
        return pd.DataFrame({"entry": (monday & down).astype(int)}, index=bars.index)


@register
class GapDownFill(Strategy):
    key = "gap_down_fill"
    name = "Relleno del hueco a la baja (intradía)"
    source = ("Estadística de relleno de huecos del S&P 500 (TradeThatSwing, 'S&P 500 / SPY gap fill strategy and "
              "statistics'): la mayoría de los huecos pequeños se cierran en el mismo día")
    summary = ("Si la acción abre al menos un 0,5% por debajo del cierre anterior, compra en la apertura. Vende cuando "
               "el precio vuelve al cierre anterior (el hueco se rellena) o, si no, al cierre del día.")
    default_symbols = ("SPY", "QQQ")
    params = {"gap_pct": 0.005}
    param_grid = {"gap_pct": [0.0025, 0.005, 0.01]}
    day_trade = True
    style = "intradía"
    notes = ("El artículo da estadísticas, no unas reglas cerradas: el tamaño mínimo del hueco (0,5%) es una elección "
             "de esta app. La compra es una orden 'limitada en la apertura': solo se ejecuta si abre al menos ese 0,5% más "
             "bajo. Sin stop: la salida es siempre ese mismo día.")

    def signals(self, bars, p):
        c = bars["close"]
        # a limit-on-open order every evening: filled only when the next open is a big enough gap down (but not a crash)
        limit = c * (1 - p["gap_pct"])
        entry = pd.Series(1, index=bars.index)
        return pd.DataFrame({"entry": entry, "entry_limit": limit, "target": c}, index=bars.index)
