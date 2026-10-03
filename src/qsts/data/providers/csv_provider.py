"""Local CSV provider: <root>/<timeframe>/<SYMBOL>.csv and <root>/actions/<SYMBOL>.csv.

Bars CSV columns: ts,open,high,low,close,volume (ts ISO-8601; naive = UTC).
Actions CSV columns: ex_date,kind,value.
Useful for vendor exports and for fully offline, reproducible datasets.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from qsts.data.bars import Timeframe
from qsts.data.providers.base import MarketDataProvider, ProviderError


class CSVProvider(MarketDataProvider):
    name = "csv"

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def supported_timeframes(self) -> set[Timeframe]:
        return {tf for tf in Timeframe if (self.root / tf.value).is_dir()}

    def get_bars(self, symbol, timeframe, start, end):
        path = self.root / timeframe.value / f"{symbol}.csv"
        if not path.exists():
            raise ProviderError(f"no file {path}")
        df = pd.read_csv(path)
        ts = pd.to_datetime(df.pop("ts"), utc=True)
        df.index = pd.DatetimeIndex(ts, name="ts")
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        s = s.tz_localize("UTC") if s.tz is None else s
        e = e.tz_localize("UTC") if e.tz is None else e
        return df[(df.index >= s) & (df.index <= e)]

    def get_corporate_actions(self, symbol):
        path = self.root / "actions" / f"{symbol}.csv"
        if not path.exists():
            return pd.DataFrame(columns=["ex_date", "kind", "value"])
        df = pd.read_csv(path)
        df["ex_date"] = pd.to_datetime(df["ex_date"], utc=True).dt.normalize()
        return df[["ex_date", "kind", "value"]]
