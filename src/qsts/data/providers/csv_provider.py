"""CSV provider: <root>/<timeframe>/<SYMBOL>.csv (index column = timestamp, OHLCV columns) and optional
<root>/actions/<SYMBOL>.csv (ex_date, kind, value). Files must contain RAW (unadjusted) prices."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from qsts.data.bars import OHLCV, Timeframe
from qsts.data.providers.base import MarketDataProvider, empty_actions, utc_bounds


class CSVProvider(MarketDataProvider):
    name = "csv"

    def __init__(self, root):
        self.root = Path(root)

    def get_bars(self, symbol: str, timeframe: Timeframe, start=None, end=None) -> pd.DataFrame:
        path = self.root / Timeframe(timeframe).value / f"{symbol.upper()}.csv"
        if not path.exists():
            raise FileNotFoundError(path)
        df = pd.read_csv(path, index_col=0)
        df.columns = [str(c).lower() for c in df.columns]
        df.index = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True), name="ts")
        s, e = utc_bounds(start, end)
        if s is not None:
            df = df[df.index >= s]
        if e is not None:
            df = df[df.index < e]
        return df[OHLCV]

    def get_corporate_actions(self, symbol: str) -> pd.DataFrame:
        path = self.root / "actions" / f"{symbol.upper()}.csv"
        if not path.exists():
            return empty_actions()
        a = pd.read_csv(path)
        a["ex_date"] = pd.to_datetime(a["ex_date"], utc=True)
        return a[["ex_date", "kind", "value"]]
