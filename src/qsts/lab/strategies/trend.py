"""Trend-following and momentum classics: Ichimoku (Goichi Hosoda), Faber's 10-month timing model, Antonacci's
momentum and Minervini's trend template."""
from __future__ import annotations

import numpy as np
import pandas as pd

from qsts.lab import ta
from qsts.lab.strategy import Strategy, register


@register
class IchimokuTK(Strategy):
    key = "ichimoku_tk"
    name = "Ichimoku: cruce Tenkan/Kijun con la nube"
    source = "Goichi Hosoda, Ichimoku Kinko Hyo (1969); regla clásica del cruce Tenkan-sen / Kijun-sen"
    summary = ("Largo cuando la Tenkan (9) cruza por encima de la Kijun (26) con el precio por encima de la nube; corto "
               "al revés. Sale con el cruce contrario o si el precio entra en el lado opuesto de la nube.")
    params = {"tenkan": 9, "kijun": 26, "senkou_b": 52}
    param_grid = {"tenkan": [7, 9, 12], "kijun": [22, 26, 30]}
    allow_short = True
    style = "semanas"
    notes = "La nube que se compara con el precio de hoy es la calculada hace 26 sesiones (como se dibuja en el gráfico)."

    def signals(self, bars, p):
        ich = ta.ichimoku(bars, p["tenkan"], p["kijun"], p["senkou_b"])
        a, b = ich["senkou_a"].shift(p["kijun"]), ich["senkou_b"].shift(p["kijun"])
        top, bottom = np.maximum(a, b), np.minimum(a, b)
        c = bars["close"]
        up, down = ta.crossover(ich["tenkan"], ich["kijun"]), ta.crossunder(ich["tenkan"], ich["kijun"])
        entry = np.where(up & (c > top), 1, np.where(down & (c < bottom), -1, 0))
        return pd.DataFrame({"entry": entry, "exit_long": down | (c < bottom), "exit_short": up | (c > top)},
                            index=bars.index)


def _monthly(c: pd.Series) -> tuple[pd.Series, pd.Series]:
    """(is last session of the month, closes of month ends only)."""
    me = ta.month_end(c.index)
    return me, c[me]


@register
class FaberTiming(Strategy):
    key = "faber_10m"
    name = "Media de 10 meses de Faber"
    source = "Mebane Faber, 'A Quantitative Approach to Tactical Asset Allocation' (Journal of Wealth Management, 2007)"
    summary = ("Una vez al mes, el último día de mercado: si el cierre está por encima de la media de los últimos 10 "
               "cierres de mes, compra (o sigue dentro); si está por debajo, vende.")
    params = {"months": 10}
    param_grid = {"months": [8, 10, 12]}
    style = "meses"

    def _gap(self, c, p):
        me, mc = _monthly(c)
        sma = mc.rolling(p["months"], min_periods=p["months"]).mean().reindex(c.index).ffill()
        return me, c / sma - 1

    def signals(self, bars, p):
        me, gap = self._gap(bars["close"], p)
        return pd.DataFrame({"entry": (me & (gap > 0)).astype(int), "exit": me & (gap <= 0)}, index=bars.index)


@register
class AntonacciMomentum(Strategy):
    key = "antonacci_momentum"
    name = "Momento de 12 meses (Antonacci)"
    source = "Gary Antonacci, 'Dual Momentum Investing' (2014): momento absoluto y relativo de 12 meses"
    summary = ("Una vez al mes: compra si la acción ha subido en los últimos 12 meses (momento absoluto). En cartera "
               "elige las que más han subido (momento relativo). Cada fin de mes se vuelve a decidir.")
    params = {"lookback": 252}
    param_grid = {"lookback": [126, 189, 252]}
    style = "meses"
    rank_rule = "la que más ha subido en 12 meses (momento relativo de Antonacci)"
    notes = ("Antonacci lo aplica a índices (EE. UU., internacional, bonos) y compara con las letras del Tesoro; aquí se "
             "aplica a acciones y se compara con 0%. Cada fin de mes se cierra y se vuelve a comprar si sigue siendo de "
             "las mejores (paga costes de entrada y salida cada mes).")

    def signals(self, bars, p):
        c = bars["close"]
        me = ta.month_end(c.index)
        mom = c / c.shift(p["lookback"]) - 1
        return pd.DataFrame({"entry": (me & (mom > 0)).astype(int), "exit": me}, index=bars.index)

    def rank(self, bars, p):
        c = bars["close"]
        return c / c.shift(p["lookback"]) - 1


@register
class MinerviniTemplate(Strategy):
    key = "minervini_template"
    name = "Plantilla de tendencia de Minervini"
    source = "Mark Minervini, 'Trade Like a Stock Market Wizard' (2013): Trend Template"
    summary = ("Compra cuando cumple la plantilla: precio por encima de las medias de 50, 150 y 200 días, la de 50 por "
               "encima de la de 150 y esta por encima de la de 200, la de 200 subiendo desde hace un mes, al menos un "
               "30% por encima del mínimo de 52 semanas y a menos de un 25% del máximo. Vende si cierra bajo la media de "
               "50 o con un stop del 8%.")
    params = {"from_low": 0.30, "from_high": 0.25, "stop_pct": 0.08}
    param_grid = {"from_low": [0.25, 0.30, 0.50], "from_high": [0.15, 0.25], "stop_pct": [0.06, 0.08, 0.10]}
    style = "meses"
    rank_rule = "la de más fuerza relativa (lo que ha subido en 12 meses), en lugar del RS de IBD"
    notes = ("El criterio 8 (RS de Investor's Business Daily ≥ 70) no se puede calcular sin IBD: en cartera se usa como "
             "orden de preferencia la subida de 12 meses. Minervini compra en rupturas de bases (VCP), que no son una "
             "regla fija: aquí se compra al cumplirse la plantilla. Salida bajo la media de 50 y stop del 8% (él "
             "recomienda cortar pérdidas antes del 10%).")

    def signals(self, bars, p):
        c = bars["close"]
        s50, s150, s200 = ta.sma(c, 50), ta.sma(c, 150), ta.sma(c, 200)
        lo, hi = ta.lowest(bars["low"], 252), ta.highest(bars["high"], 252)
        ok = ((c > s150) & (c > s200) & (s150 > s200) & (s200 > s200.shift(22)) & (s50 > s150) & (s50 > s200)
              & (c > s50) & (c >= lo * (1 + p["from_low"])) & (c >= hi * (1 - p["from_high"])))
        return pd.DataFrame({"entry": ok.astype(int), "exit": c < s50, "stop_pct": p["stop_pct"]}, index=bars.index)

    def rank(self, bars, p):
        c = bars["close"]
        return c / c.shift(252) - 1
