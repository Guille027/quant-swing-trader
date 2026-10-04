"""Yahoo Finance provider (via the unofficial `yfinance` package; install with `.[yahoo]`).

Verified behaviour of Yahoo data (checked against live responses, see docs/STATUS.md):
- With auto_adjust=False, Open/High/Low/Close are already SPLIT-adjusted for every later split, volume is
  split-adjusted the opposite way, and cash dividends are split-adjusted too. Only "Adj Close" includes dividends.
- This provider therefore reconstructs RAW prices (price x product of later split ratios), raw volume and raw
  dividends, so that the repository stores raw data and qsts.data.adjust applies adjustments explicitly.
  `verify_roundtrip` checks adjust(raw, actions, "total") against Yahoo's own Adj Close.
Yahoo is an unofficial, best-effort source: no SLA, occasional gaps/revisions. Treat it as research-grade only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from qsts.data.bars import OHLCV, Timeframe
from qsts.data.providers.base import MarketDataProvider, empty_actions, empty_earnings, utc_bounds

_INTERVAL = {Timeframe.D1: "1d", Timeframe.W1: "1wk", Timeframe.H1: "1h"}


def _local_dates(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Exchange-local calendar date of each timestamp (Yahoo stamps daily bars/actions in exchange time)."""
    idx = pd.DatetimeIndex(idx)
    return (idx.tz_localize(None) if idx.tz is not None else idx).normalize()


class YFinanceProvider(MarketDataProvider):
    name = "yahoo"

    def __init__(self, timeout: int = 30):
        import yfinance  # noqa: F401  (fail early with a clear ImportError)
        self.timeout = timeout
        self._actions: dict[str, pd.DataFrame] = {}

    def _ticker(self, symbol: str):
        import yfinance as yf
        return yf.Ticker(symbol)

    def _yahoo_actions(self, symbol: str) -> pd.DataFrame:
        """Full action history as Yahoo reports it (dividends split-adjusted), indexed by local ex-date."""
        if symbol not in self._actions:
            a = self._ticker(symbol).actions
            if a is None or len(a) == 0:
                a = pd.DataFrame(columns=["Dividends", "Stock Splits"], dtype="float64")
            a = a.copy()
            a.index = _local_dates(a.index)
            self._actions[symbol] = a
        return self._actions[symbol]

    def _future_split_factor(self, symbol: str, dates: pd.DatetimeIndex) -> np.ndarray:
        """Product of split ratios with ex-date strictly after each date (Yahoo's split-adjustment divisor)."""
        a = self._yahoo_actions(symbol)
        splits = a.loc[a.get("Stock Splits", pd.Series(dtype=float)).fillna(0) > 0, "Stock Splits"] \
            if "Stock Splits" in a else pd.Series(dtype=float)
        f = np.ones(len(dates))
        for ex, r in splits.items():
            f[dates < ex] *= float(r)
        return f

    def _history(self, symbol: str, timeframe: Timeframe, start, end) -> pd.DataFrame:
        if timeframe not in _INTERVAL:
            raise ValueError(f"Yahoo has no native {timeframe.value} bars; build 4H from 1H with resample_intraday_to_4h")
        s, e = utc_bounds(start, end)
        h = self._ticker(symbol).history(start=None if s is None else s.strftime("%Y-%m-%d"),
                                         end=None if e is None else e.strftime("%Y-%m-%d"),
                                         interval=_INTERVAL[timeframe], auto_adjust=False, actions=False,
                                         back_adjust=False, repair=False, keepna=False, timeout=self.timeout,
                                         raise_errors=True)
        if h is None or h.empty:
            raise ValueError(f"Yahoo returned no data for {symbol}")
        return h

    def get_bars(self, symbol: str, timeframe: Timeframe, start=None, end=None) -> pd.DataFrame:
        timeframe = Timeframe(timeframe)
        h = self._history(symbol, timeframe, start, end)
        f = self._future_split_factor(symbol, _local_dates(h.index))
        out = pd.DataFrame({"open": h["Open"].values * f, "high": h["High"].values * f, "low": h["Low"].values * f,
                            "close": h["Close"].values * f, "volume": h["Volume"].values / f}, index=h.index)
        out.index.name = "ts"
        return out[OHLCV]

    def get_earnings(self, symbol: str, limit: int = 100) -> pd.DataFrame:
        """Yahoo earnings calendar (yfinance scrapes finance.yahoo.com/calendar/earnings; up to 100 rows = ~25 years,
        incl. upcoming dates). Times have hour precision in America/New_York; "12 AM" means the time is unknown.
        Surprise(%) is in percent. UNVERIFIED from the build environment (host blocked): check coverage in the app."""
        df = self._ticker(symbol).get_earnings_dates(limit=limit)
        if df is None or len(df) == 0:
            return empty_earnings()
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("America/New_York") if idx.tz is None else idx.tz_convert("America/New_York")
        col = lambda name: pd.to_numeric(df[name], errors="coerce").to_numpy() if name in df else np.nan  # noqa: E731
        out = pd.DataFrame({"announced_at": idx.tz_convert("UTC"),
                            "time_known": ~((idx.hour == 0) & (idx.minute == 0)),
                            "eps_estimate": col("EPS Estimate"), "eps_reported": col("Reported EPS"),
                            "surprise_pct": col("Surprise(%)")})
        return out.drop_duplicates("announced_at").sort_values("announced_at").reset_index(drop=True)

    def get_fx(self, pair: str = "EURUSD=X", period: str = "3mo") -> pd.Series:
        """Daily closes of a Yahoo FX pair (EURUSD=X = US dollars per euro), indexed by date (UTC midnight)."""
        h = self._ticker(pair).history(period=period, interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return pd.Series(dtype=float)
        idx = pd.DatetimeIndex(h.index)
        local = idx if idx.tz is None else idx.tz_localize(None)  # the pair's own trading date
        days = local.normalize().tz_localize("UTC")
        return pd.Series(h["Close"].to_numpy(dtype=float), index=days).dropna()

    def get_corporate_actions(self, symbol: str) -> pd.DataFrame:
        a = self._yahoo_actions(symbol)
        if a.empty:
            return empty_actions()
        rows = []
        if "Stock Splits" in a:
            for ex, r in a["Stock Splits"].items():
                if r and r > 0:
                    rows.append((ex, "split", float(r)))
        if "Dividends" in a:
            divs = a["Dividends"][a["Dividends"].fillna(0) > 0]
            raw = divs.values * self._future_split_factor(symbol, pd.DatetimeIndex(divs.index))
            rows += [(ex, "dividend", float(v)) for ex, v in zip(divs.index, raw)]
        out = pd.DataFrame(rows, columns=["ex_date", "kind", "value"])
        out["ex_date"] = pd.to_datetime(out["ex_date"]).dt.tz_localize("UTC")
        return out.sort_values(["ex_date", "kind"], kind="stable").reset_index(drop=True)

    def verify_roundtrip(self, symbol: str, start=None, end=None) -> dict:
        """Compare adjust(raw, actions, 'total') close with Yahoo's Adj Close over [start, end]."""
        from qsts.data.adjust import adjust
        from qsts.data.bars import normalize_index
        h = self._history(symbol, Timeframe.D1, start, end)
        raw = normalize_index(self.get_bars(symbol, Timeframe.D1, start, end), Timeframe.D1)
        ours = adjust(raw, self.get_corporate_actions(symbol), "total")["close"]
        ref = pd.Series(h["Adj Close"].values, index=normalize_index(h, Timeframe.D1).index)
        # Yahoo's Adj Close is normalised to the latest bar; compare shapes after aligning the last value
        ours, ref = ours.align(ref, join="inner")
        scale = ref.iloc[-1] / ours.iloc[-1]
        rel = (ours * scale / ref - 1).abs()
        return {"symbol": symbol, "bars": int(len(rel)), "max_rel_err": float(rel.max()),
                "median_rel_err": float(rel.median()), "scale_last": float(scale)}
