"""The 'overnight' effect: holding stocks only from the close to the next open (popular on social media; studied by
Cooper, Cliff and Gulen, 'Return differences between trading and non-trading hours: Like night and day', 2008)."""
from __future__ import annotations

import pandas as pd

from qsts.lab.strategy import Strategy, register


@register
class OvernightHold(Strategy):
    key = "overnight_hold"
    name = "Comprar al cierre, vender en la apertura"
    source = ("Idea difundida en redes (TikTok); estudiada por Cooper, Cliff y Gulen, 'Return differences between "
              "trading and non-trading hours: Like night and day' (2008)")
    summary = ("Cada día compra justo al cierre y vende en la apertura del día siguiente: solo se queda la subida o "
               "bajada de la noche.")
    default_symbols = ("SPY", "QQQ")
    params = {}
    overnight = True
    style = "pocos días"
    notes = ("Se compra en la subasta de cierre y se vende en la de apertura siguiente. Opera todos los días, así que "
             "los costes pesan mucho: el backtest cobra el deslizamiento en cada compra y en cada venta. De momento "
             "solo se puede probar en el backtest, todavía no en paper.")

    def signals(self, bars, p):
        return pd.DataFrame({"entry": 1}, index=bars.index)
