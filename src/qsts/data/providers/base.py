"""Market-data provider abstraction. Providers return RAW bars; validation happens in qsts.data.quality."""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from qsts.data.bars import Timeframe

ACTION_COLUMNS = ["ex_date", "kind", "value"]


def empty_actions() -> pd.DataFrame:
    return pd.DataFrame({"ex_date": pd.DatetimeIndex([], tz="UTC"), "kind": pd.Series([], dtype=object),
                         "value": pd.Series([], dtype="float64")})


def empty_earnings() -> pd.DataFrame:
    return pd.DataFrame({"announced_at": pd.DatetimeIndex([], tz="UTC"), "time_known": pd.Series([], dtype=bool),
                         "eps_estimate": pd.Series([], dtype=float), "eps_reported": pd.Series([], dtype=float),
                         "surprise_pct": pd.Series([], dtype=float)})


def utc_bounds(start, end) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """(start, exclusive end). A date-only `end` includes that whole day."""
    def u(t):
        if t is None:
            return None
        ts = pd.Timestamp(t)
        return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    s, e = u(start), u(end)
    if e is not None and e == e.normalize():
        e = e + pd.Timedelta(days=1)
    return s, e


class MarketDataProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: Timeframe, start, end) -> pd.DataFrame:
        """Raw OHLCV (lower-case columns), DatetimeIndex = bar open (or session date for daily bars).
        `end` is inclusive. Must never fill, interpolate or otherwise fabricate bars."""

    def get_earnings(self, symbol: str) -> pd.DataFrame:
        """Columns announced_at (UTC), time_known, eps_estimate, eps_reported, surprise_pct (in %).
        Includes upcoming (not yet reported) dates. Empty when the provider has none."""
        return empty_earnings()

    def get_corporate_actions(self, symbol: str) -> pd.DataFrame:
        """Columns ex_date (UTC), kind ('split' | 'dividend'), value (split ratio | RAW cash per share)."""
        return empty_actions()
