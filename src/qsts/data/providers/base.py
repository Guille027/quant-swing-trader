"""MarketDataProvider abstraction. The rest of the system never imports a concrete provider."""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from qsts.data.bars import Timeframe


class ProviderError(RuntimeError):
    pass


class MarketDataProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def supported_timeframes(self) -> set[Timeframe]: ...

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: Timeframe, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Return RAW (unadjusted) OHLCV with a UTC DatetimeIndex of bar open times."""

    @abstractmethod
    def get_corporate_actions(self, symbol: str) -> pd.DataFrame:
        """Return DataFrame[ex_date (UTC Timestamp), kind ('split'|'dividend'), value]."""
