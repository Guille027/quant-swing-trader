"""Yahoo Finance via the `yfinance` package (free, unofficial).

Status: IMPLEMENTED but NOT verified against the live service from the build environment
(network egress to Yahoo was blocked there). Known limitations of the source:
- 1h history limited to ~730 days; no native 4h (built from 1h via resample).
- Unofficial API; may change or rate-limit without notice.
- No point-in-time universe data -> use a separate membership source.
"""
from __future__ import annotations

import pandas as pd

from qsts.data.bars import Timeframe
from qsts.data.providers.base import MarketDataProvider, ProviderError

_INTERVAL = {Timeframe.H1: "1h", Timeframe.D1: "1d", Timeframe.W1: "1wk"}


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"

    def __init__(self):
        try:
            import yfinance  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise ProviderError("pip install qsts[yahoo]") from e

    def supported_timeframes(self):
        return set(_INTERVAL)

    def get_bars(self, symbol, timeframe, start, end):
        import yfinance as yf
        if timeframe not in _INTERVAL:
            raise ProviderError(f"{timeframe} not supported natively; resample from 1h")
        df = yf.Ticker(symbol).history(
            start=pd.Timestamp(start).strftime("%Y-%m-%d"),
            end=(pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            interval=_INTERVAL[timeframe], auto_adjust=False, actions=False,
            prepost=False, raise_errors=True,
        )
        if df.empty:
            raise ProviderError(f"no data for {symbol}")
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        idx = pd.DatetimeIndex(df.index)
        if timeframe is Timeframe.H1:
            df.index = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
        else:
            # Yahoo stamps daily/weekly bars at exchange-local midnight; keep the session DATE.
            local = idx.tz_localize(None) if idx.tz is not None else idx
            df.index = pd.DatetimeIndex(local.normalize()).tz_localize("UTC")
        return df

    def get_corporate_actions(self, symbol):
        import yfinance as yf
        t = yf.Ticker(symbol)
        rows = []
        for kind, s in (("split", t.splits), ("dividend", t.dividends)):
            for ts, v in s.items():
                d = pd.Timestamp(ts).tz_localize(None).normalize().tz_localize("UTC")
                rows.append({"ex_date": d, "kind": kind, "value": float(v)})
        return pd.DataFrame(rows, columns=["ex_date", "kind", "value"])
