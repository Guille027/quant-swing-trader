"""Data Quality Engine: DOWNLOAD -> VALIDATE -> CLEAN -> NORMALIZE -> STORE -> USE.

`validate_and_clean` is the only constructor of `ValidatedBars`. Strategy, feature and
backtest code accept `ValidatedBars`, so un-validated data cannot reach them by accident.

Cleaning never fabricates data: missing sessions are reported, not forward-filled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from qsts.core.hashing import hash_frame
from qsts.data.bars import OHLCV, Timeframe, nyse_sessions, to_canonical


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"  # data unusable


@dataclass
class Issue:
    code: str
    severity: Severity
    message: str
    timestamps: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QualityConfig:
    # A single-bar move larger than this (in log-return) is flagged as a possible
    # unadjusted split / bad tick. 0.4 ~ +/-49%: rare for S&P 500 members on one bar,
    # but common for a 2:1 split (-69%). Flag only; never auto-"fixed".
    extreme_move_log_return: float = 0.4
    # Daily data whose last bar is more than this many sessions old is stale.
    stale_after_sessions: int = 3
    # Missing-session ratio above which the series is rejected outright.
    max_missing_ratio: float = 0.02
    # Consecutive zero-volume bars considered suspicious (halt / dead feed).
    zero_volume_run: int = 3


@dataclass
class QualityReport:
    symbol: str
    timeframe: Timeframe
    issues: list[Issue] = field(default_factory=list)
    rows_in: int = 0
    rows_out: int = 0

    @property
    def ok(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    def add(self, code: str, sev: Severity, msg: str, ts=None) -> None:
        self.issues.append(Issue(code, sev, msg, [str(t) for t in (ts if ts is not None else [])][:50]))

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}


class ValidatedBars:
    """Immutable, validated canonical bars. Only created by `validate_and_clean`."""

    __slots__ = ("_df", "symbol", "timeframe", "report", "version")
    _token = object()

    def __init__(self, df: pd.DataFrame, symbol: str, timeframe: Timeframe, report: QualityReport, _tok=None):
        if _tok is not ValidatedBars._token:
            raise TypeError("ValidatedBars can only be created by the data quality engine")
        self._df = df
        self.symbol = symbol
        self.timeframe = timeframe
        self.report = report
        self.version = hash_frame(df)

    @property
    def df(self) -> pd.DataFrame:
        return self._df.copy()

    def __len__(self) -> int:
        return len(self._df)


class DataQualityError(RuntimeError):
    def __init__(self, report: QualityReport):
        self.report = report
        errs = "; ".join(f"{i.code}: {i.message}" for i in report.issues if i.severity is Severity.ERROR)
        super().__init__(f"{report.symbol} {report.timeframe.value} rejected: {errs}")


def validate_and_clean(
    raw: pd.DataFrame, symbol: str, timeframe: Timeframe,
    cfg: QualityConfig = QualityConfig(), asof: pd.Timestamp | None = None,
) -> ValidatedBars:
    rep = QualityReport(symbol, timeframe, rows_in=len(raw))
    if raw is None or raw.empty:
        rep.add("EMPTY", Severity.ERROR, "no data")
        raise DataQualityError(rep)

    idx = pd.DatetimeIndex(raw.index)
    if idx.tz is None:
        rep.add("NAIVE_TIMESTAMPS", Severity.WARNING, "timezone-naive timestamps assumed UTC")
    df = to_canonical(raw, timeframe)

    # --- timestamps ---------------------------------------------------------
    if not df.index.is_monotonic_increasing:
        rep.add("UNSORTED", Severity.WARNING, "timestamps not sorted; sorted")
        df = df.sort_index(kind="stable")

    dup_mask = df.index.duplicated(keep=False)
    if dup_mask.any():
        dups = df[dup_mask]
        conflicting = dups.groupby(level=0)[OHLCV].nunique().gt(1).any(axis=1)
        if conflicting.any():
            rep.add("CONFLICTING_DUPLICATES", Severity.ERROR,
                    f"{int(conflicting.sum())} timestamps with conflicting values",
                    conflicting[conflicting].index)
        rep.add("DUPLICATES", Severity.WARNING, f"{int(df.index.duplicated().sum())} duplicate rows removed",
                df.index[df.index.duplicated()])
        df = df[~df.index.duplicated(keep="first")]

    if asof is not None:
        future = df.index > pd.Timestamp(asof)
        if future.any():
            rep.add("FUTURE_TIMESTAMPS", Severity.ERROR, f"{int(future.sum())} bars after asof", df.index[future])

    invalid_session = df["available_at"].isna()
    if invalid_session.any():
        rep.add("NON_SESSION_BARS", Severity.WARNING,
                f"{int(invalid_session.sum())} bars outside NYSE sessions removed", df.index[invalid_session])
        df = df[~invalid_session]

    # --- values -------------------------------------------------------------
    nan_rows = df[OHLCV].isna().any(axis=1)
    if nan_rows.any():
        rep.add("NAN_VALUES", Severity.WARNING, f"{int(nan_rows.sum())} rows with NaN removed", df.index[nan_rows])
        df = df[~nan_rows]

    o, h, l, c, v = (df[k] for k in OHLCV)
    impossible = (
        (h < np.maximum(o, c)) | (l > np.minimum(o, c)) | (h < l)
        | (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0) | (v < 0)
    )
    if impossible.any():
        rep.add("IMPOSSIBLE_OHLC", Severity.WARNING, f"{int(impossible.sum())} impossible bars removed",
                df.index[impossible])
        df = df[~impossible]

    if df.empty:
        rep.add("EMPTY_AFTER_CLEAN", Severity.ERROR, "no valid rows left")
        raise DataQualityError(rep)

    logret = np.log(df["close"]).diff().abs()
    extreme = logret > cfg.extreme_move_log_return
    if extreme.any():
        rep.add("EXTREME_MOVE", Severity.WARNING,
                "possible unadjusted split or bad tick; verify corporate actions", df.index[extreme])

    zero = (df["volume"] == 0).astype(int)
    runs = zero.groupby((zero != zero.shift()).cumsum()).transform("sum") * zero
    if (runs >= cfg.zero_volume_run).any():
        rep.add("ZERO_VOLUME_RUN", Severity.WARNING, "consecutive zero-volume bars", df.index[runs >= cfg.zero_volume_run])

    # --- completeness (daily only: calendar is exact) ------------------------
    if timeframe is Timeframe.D1:
        expected = nyse_sessions(df.index.min(), df.index.max())
        missing = expected.difference(df.index)
        if len(missing):
            ratio = len(missing) / max(len(expected), 1)
            sev = Severity.ERROR if ratio > cfg.max_missing_ratio else Severity.WARNING
            rep.add("MISSING_SESSIONS", sev, f"{len(missing)} missing sessions ({ratio:.2%})", missing)
        if asof is not None and pd.Timestamp(asof) >= df.index.max():
            recent = nyse_sessions(df.index.max(), pd.Timestamp(asof))
            lag = len(recent) - 1
            if lag > cfg.stale_after_sessions:
                rep.add("STALE", Severity.ERROR, f"last bar is {lag} sessions old")
    elif timeframe is Timeframe.H1:
        per_day = df.groupby(df.index.normalize()).size()
        short = per_day[per_day < 4]
        if len(short):
            rep.add("SHORT_SESSIONS", Severity.INFO, f"{len(short)} sessions with <4 hourly bars", short.index)

    rep.rows_out = len(df)
    if not rep.ok:
        raise DataQualityError(rep)
    df.flags.allows_duplicate_labels = False
    return ValidatedBars(df, symbol, timeframe, rep, _tok=ValidatedBars._token)
