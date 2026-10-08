"""Toby Crabel, 'Day Trading with Short Term Price Patterns and Opening Range Breakout' (1990)."""
from __future__ import annotations

import pandas as pd

from qsts.lab import ta
from qsts.lab.strategy import Strategy, register


@register
class NR7Breakout(Strategy):
    key = "nr7_breakout"
    name = "NR7 de Crabel (intradía)"
    source = "Toby Crabel, 'Day Trading with Short Term Price Patterns and Opening Range Breakout' (1990): patrón NR7"
    summary = ("Cuando un día tiene el rango (máximo − mínimo) más estrecho de los últimos 7, al día siguiente deja una "
               "orden de compra stop en su máximo. Si se activa, el stop de protección va en su mínimo y la posición se "
               "cierra al final del día.")
    default_symbols = ("SPY", "QQQ")
    params = {"days": 7}
    param_grid = {"days": [4, 7, 10]}
    day_trade = True
    style = "intradía"
    notes = ("Crabel opera la ruptura en las dos direcciones (la primera que se active); aquí solo la compra. La salida "
             "al cierre del mismo día es la de sus operaciones intradía.")

    def signals(self, bars, p):
        rng = bars["high"] - bars["low"]
        nr = rng <= ta.lowest(rng, p["days"])
        return pd.DataFrame({"entry": nr.astype(int), "entry_stop": bars["high"].where(nr),
                             "stop": bars["low"].where(nr)}, index=bars.index)
