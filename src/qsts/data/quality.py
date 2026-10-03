"""Data-quality engine. The ONLY way to obtain `ValidatedBars`, the only bar type the repository stores.

Problems are reported, never papered over: invalid rows are removed (and counted), missing sessions are
reported and NEVER filled, and anything that would make research unreliable raises DataQualityError.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from qsts.core.hashing import hash_frame
from qsts.data.bars import OHLCV, Timeframe, normalize_index, nyse_schedule, to_canonical


class Severity(str, Enum):
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: Severity
    message: str
    count: int = 0


@dataclass
class QualityReport:
    symbol: str
    timeframe: Timeframe
    rows_in: int = 0
    rows_out: int = 0
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}

    def add(self, code: str, severity: Severity, message: str, count: int = 0) -> None:
        self.issues.append(QualityIssue(code, severity, message, count))

    def __str__(self) -> str:
        return "; ".join(f"{i.severity.value} {i.code}: {i.message}" for i in self.issues) or "OK"


class DataQualityError(Exception):
    def __init__(self, report: QualityReport):
        self.report = report
        errs = [f"{i.code}: {i.message}" for i in report.issues if i.severity is Severity.ERROR]
        super().__init__(f"{report.symbol} {report.timeframe.value}: " + "; ".join(errs))


@dataclass(frozen=True)
class QualityConfig:
    max_removed_fraction: float = 0.01   # invalid rows removed above this -> ERROR
    max_missing_fraction: float = 0.01   # missing sessions inside the range above this -> ERROR
    max_missing_gap: int = 5             # longest run of consecutive missing sessions -> ERROR above this
    max_stale_sessions: int = 3          # completed sessions after the last bar (only checked with asof)
    extreme_log_move: float = 0.35       # |log close-to-close| above this -> WARNING (unadjusted split?)
    zero_volume_run: int = 5             # consecutive zero-volume bars -> WARNING
    price_tolerance: float = 1e-6        # relative tolerance for OHLC consistency (rounding in vendor data)


_TOKEN = object()


class ValidatedBars:
    """Bars that passed the quality engine. Cannot be constructed elsewhere."""

    __slots__ = ("_df", "symbol", "timeframe", "report", "_version")

    def __init__(self, df: pd.DataFrame, symbol: str, timeframe: Timeframe, report: QualityReport, *, _token=None):
        if _token is not _TOKEN:
            raise TypeError("ValidatedBars can only be created by qsts.data.quality.validate_and_clean")
        self._df, self.symbol, self.timeframe, self.report = df, symbol, timeframe, report
        self._version: str | None = None

    @property
    def df(self) -> pd.DataFrame:
        return self._df.copy()

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = hash_frame(self._df)
        return self._version

    def __len__(self) -> int:
        return len(self._df)

    def __repr__(self) -> str:
        return f"ValidatedBars({self.symbol}, {self.timeframe.value}, n={len(self)}, issues={sorted(self.report.codes())})"


def _longest_run(mask: np.ndarray) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def _fmt_dates(idx, n: int = 5) -> str:
    s = [str(pd.Timestamp(t).date()) for t in list(idx)[:n]]
    return ", ".join(s) + (" ..." if len(idx) > n else "")


def validate_and_clean(df: pd.DataFrame, symbol: str, timeframe: Timeframe, cfg: QualityConfig = QualityConfig(),
                       asof=None) -> ValidatedBars:
    """Validate raw OHLCV bars. `asof` = the decision time the data is used for (default: now, UTC);
    it drives the FUTURE_TIMESTAMPS / INCOMPLETE_BAR checks and, only when given, the STALE check."""
    timeframe = Timeframe(timeframe)
    rep = QualityReport(symbol, timeframe, rows_in=len(df))
    now = pd.Timestamp.now(tz="UTC")
    t_ref = now if asof is None else (pd.Timestamp(asof).tz_localize("UTC") if pd.Timestamp(asof).tz is None
                                      else pd.Timestamp(asof).tz_convert("UTC"))
    missing_cols = [c for c in OHLCV if c not in [str(c).lower() for c in df.columns]]
    if missing_cols:
        rep.add("MISSING_COLUMNS", Severity.ERROR, f"missing {missing_cols}")
        raise DataQualityError(rep)
    if df.empty:
        rep.add("EMPTY", Severity.ERROR, "no bars")
        raise DataQualityError(rep)
    d = normalize_index(df, timeframe)[OHLCV].astype("float64")

    # duplicates: identical rows are dropped; same timestamp with different values cannot be resolved
    exact = d.reset_index().duplicated(keep="first").values
    if exact.any():
        rep.add("DUPLICATES", Severity.WARNING, f"{int(exact.sum())} identical duplicate bars removed", int(exact.sum()))
        d = d[~exact]
    conflicting = d.index.duplicated(keep=False)
    if conflicting.any():
        rep.add("CONFLICTING_DUPLICATES", Severity.ERROR,
                f"{int(d.index[conflicting].nunique())} timestamps with conflicting values: {_fmt_dates(d.index[conflicting].unique())}")
        raise DataQualityError(rep)

    if d.index.max() > t_ref:
        fut = d.index[d.index > t_ref]
        rep.add("FUTURE_TIMESTAMPS", Severity.ERROR, f"{len(fut)} bars start after {t_ref}: {_fmt_dates(fut)}", len(fut))
        raise DataQualityError(rep)

    removed = 0
    nan = d.isna().any(axis=1).values
    if nan.any():
        rep.add("NAN_VALUES", Severity.WARNING, f"{int(nan.sum())} bars with NaN removed: {_fmt_dates(d.index[nan])}", int(nan.sum()))
        d, removed = d[~nan], removed + int(nan.sum())

    tol = 1 + cfg.price_tolerance
    o, h, l, c, v = (d[k].values for k in OHLCV)
    bad = ((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0) | (v < 0) | (h * tol < np.maximum.reduce([o, c, l]))
           | (l > np.minimum.reduce([o, c, h]) * tol))
    if bad.any():
        rep.add("IMPOSSIBLE_OHLC", Severity.WARNING, f"{int(bad.sum())} impossible bars removed: {_fmt_dates(d.index[bad])}", int(bad.sum()))
        d, removed = d[~bad], removed + int(bad.sum())

    sched = nyse_schedule(d.index.min() - pd.Timedelta(days=1), d.index.max() + pd.Timedelta(days=1))
    if timeframe.intraday:
        sess = d.index.tz_convert("America/New_York").tz_localize(None).normalize().tz_localize("UTC")
        opens = sched["market_open"].reindex(sess).values
        closes = sched["market_close"].reindex(sess).values
        ok = pd.notna(opens) & (d.index.values >= opens) & (d.index.values < closes)
    elif timeframe is Timeframe.D1:
        ok = d.index.isin(sched.index)
    else:
        ok = d.index.dayofweek <= 4
    if (~ok).any():
        n = int((~ok).sum())
        rep.add("NON_SESSION_BARS", Severity.WARNING, f"{n} bars outside NYSE sessions removed: {_fmt_dates(d.index[~ok])}", n)
        d, removed = d[ok], removed + n

    if rep.rows_in and removed / rep.rows_in > cfg.max_removed_fraction:
        rep.add("TOO_MANY_INVALID_ROWS", Severity.ERROR, f"{removed}/{rep.rows_in} rows removed")
    if d.empty:
        rep.add("EMPTY", Severity.ERROR, "no valid bars left")
        raise DataQualityError(rep)

    canon = to_canonical(d, timeframe)
    incomplete = (canon["available_at"] > t_ref).values
    if incomplete.any():
        rep.add("INCOMPLETE_BAR", Severity.WARNING,
                f"{int(incomplete.sum())} bar(s) not complete at {t_ref} removed: {_fmt_dates(canon.index[incomplete])}",
                int(incomplete.sum()))
        canon = canon[~incomplete]
        if canon.empty:
            rep.add("EMPTY", Severity.ERROR, "no complete bars")
            raise DataQualityError(rep)

    if timeframe in (Timeframe.D1,) or timeframe.intraday:
        dates = canon.index if timeframe is Timeframe.D1 else \
            canon.index.tz_convert("America/New_York").tz_localize(None).normalize().tz_localize("UTC").unique()
        expected = sched.index[(sched.index >= dates.min()) & (sched.index <= dates.max())]
        miss = ~expected.isin(dates)
        if miss.any():
            n, gap = int(miss.sum()), _longest_run(miss)
            sev = Severity.ERROR if (n / len(expected) > cfg.max_missing_fraction or gap > cfg.max_missing_gap) \
                else Severity.WARNING
            rep.add("MISSING_SESSIONS", sev,
                    f"{n}/{len(expected)} sessions missing (longest gap {gap}); NOT filled: {_fmt_dates(expected[miss])}", n)

    if asof is not None:
        last = canon["available_at"].iloc[-1]
        later = nyse_schedule(canon.index[-1], t_ref)
        stale = int(((later["market_close"] > last) & (later["market_close"] <= t_ref)).sum())
        if stale > cfg.max_stale_sessions:
            rep.add("STALE", Severity.ERROR, f"last bar {canon.index[-1].date()} is {stale} sessions old at {t_ref}", stale)

    lr = np.log(canon["close"]).diff().abs()
    ext = (lr > cfg.extreme_log_move).values
    if ext.any():
        rep.add("EXTREME_MOVE", Severity.WARNING,
                f"{int(ext.sum())} close-to-close moves > {cfg.extreme_log_move:.2f} log (unadjusted split?): "
                f"{_fmt_dates(canon.index[ext])}", int(ext.sum()))

    run = _longest_run((canon["volume"] == 0).values)
    if run >= cfg.zero_volume_run:
        rep.add("ZERO_VOLUME", Severity.WARNING, f"run of {run} zero-volume bars", run)

    rep.rows_out = len(canon)
    if not rep.ok:
        raise DataQualityError(rep)
    return ValidatedBars(canon, symbol, timeframe, rep, _token=_TOKEN)
