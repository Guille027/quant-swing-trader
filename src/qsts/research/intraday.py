"""Intraday lab: opening-range breakout (ORB) rules on 5-minute or 1-hour bars, long and/or short.

Rule family (at most one trade per stock and day, always flat at the close):
- opening range = high/low of the first `range_min` minutes of the session;
- entry when a later bar breaks the range: `close` = a bar CLOSES beyond it and the trade starts at the next bar's
  open; `touch` = a stop order at the range edge (filled at the edge, or at the bar's open if it gaps through);
- stop at the other edge or at the middle of the range; optional profit target in R (multiples of the risk);
  entries only during the `cutoff_min` minutes after the range;
- optional context filters (range size vs the daily ATR, opening volume vs its own average, opening gap, daily
  trend): a price pattern combined with its context.
Fill conventions are conservative: a bar that reaches both stop and target counts as a stop; a `touch` entry
whose own bar also reaches the stop is stopped out; a bar that breaks both edges is skipped (order unknown).

Point in time: daily context (ATR %, trend) is joined at the session open through `align_higher_timeframe`, so
only the previous session's values are visible; the gap uses the previous stored session's last bar (only when
it is the previous NYSE session); the opening volume is compared with the previous sessions only.

Discipline, the same as the daily search: each dataset has an out-of-sample boundary stored in the database the
first time it is used (the last 25% of the sessions stored then). The search never loads those sessions. A new
epoch (later boundary over fresh, unseen sessions) starts automatically only when the stored history has at
least doubled. The search uses the first 80% of the research part; the last 20% is the pre-exam. The score is the
worst block Sharpe over all stocks and two random halves of them, minus a complexity penalty. Every evaluated
rule is stored and counted (Fiabilidad / DSR). The final test opens the boundary once per rule and epoch and is
judged like the daily one (makes money, beats holding the same stocks, keeps half of its research Sharpe).

Data reality (Yahoo, checked 2026-10-05): 5-minute bars only for the last 60 sessions, 1-hour bars for about
730 sessions. Stored bars are kept, so history grows if data is updated at least every ~50 days. Validation
and the final test are only offered with enough sessions.
"""
from __future__ import annotations

import threading
import warnings
import zlib
from collections import deque
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from typing import Callable, ClassVar

import numpy as np
import pandas as pd
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from qsts.core.hashing import hash_obj
from qsts.data.bars import Timeframe, align_higher_timeframe, nyse_schedule
from qsts.db import models as m
from qsts.research.autoresearch import final_verdict
from qsts.research.experiments import _clean
from qsts.research.scoring import expected_max_sharpe, psr_from_stats, return_stats

DATASETS = {"5m": Timeframe.M5, "1h": Timeframe.H1}
BAR_MIN = {"5m": 5, "1h": 60}
OOS_SHARE, PRE_EXAM_SHARE = 0.25, 0.20
MIN_SEARCH_SESSIONS, MIN_OOS_SESSIONS = 120, 60  # below this, results are exploration only
EXIT_KIND = {0: "cierre", 1: "stop", 2: "objetivo"}

# the search space (discrete: every rule is a point of this grid)
SPACE = {
    "5m": {"range_min": [5, 15, 30, 60], "cutoff_min": [30, 60, 120, 240]},
    "1h": {"range_min": [60], "cutoff_min": [60, 120, 180, 300]},
}
COMMON = {"direction": ["long", "short", "both"], "confirm": ["close", "touch"], "stop": ["range", "mid"],
          "target_r": [0.0, 1.0, 1.5, 2.0, 3.0], "min_range_atr": [0.0, 0.1, 0.2, 0.3],
          "max_range_atr": [99.0, 0.5, 1.0], "min_rel_vol": [0.0, 1.0, 1.5, 2.0], "gap_min": [-1.0, 0.0, 0.005, 0.01],
          "gap_max": [1.0, 0.0, -0.005], "trend": ["any", "with", "against"]}
FILTERS_OFF = {"min_range_atr": 0.0, "max_range_atr": 99.0, "min_rel_vol": 0.0, "gap_min": -1.0, "gap_max": 1.0,
               "trend": "any"}


# the other pattern families (each rule is a point of its family's grid)
GAP_SPACE = {"mode": ["fade", "go"], "min_gap": [0.005, 0.01, 0.02, 0.03], "max_gap": [0.02, 0.04, 1.0],
             "side": ["both", "up", "down"], "confirm": [False, True], "stop_atr": [0.25, 0.5, 1.0],
             "target": ["close", "fill", "1R", "2R"], "trend": ["any", "with", "against"]}
GAP_ENTRY = {"5m": [5, 15, 30, 60], "1h": [60, 120]}
VWAP_SPACE = {"mode": ["cross", "revert"], "direction": ["long", "short", "both"], "stretch_atr": [0.25, 0.5, 0.75, 1.0],
              "stop_atr": [0.25, 0.5, 1.0], "target": ["close", "vwap", "1R", "2R"], "trend": ["any", "with", "against"]}
VWAP_TIME = {"5m": {"start_min": [15, 30, 60], "window_min": [60, 120, 240]},
             "1h": {"start_min": [60, 120], "window_min": [60, 120, 240]}}
FAMILY_NAMES = {"orb": "rango de apertura", "gap": "hueco de apertura", "vwap": "VWAP"}
OPTIONAL = {"orb": FILTERS_OFF,  # settings a new random rule usually leaves off (simple first)
            "gap": {"max_gap": 1.0, "side": "both", "confirm": False, "target": "close", "trend": "any"},
            "vwap": {"target": "close", "trend": "any"}}


def _py(v):
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    return float(v) if isinstance(v, (float, np.floating)) else int(v) if isinstance(v, (int, np.integer)) else str(v)


def _n(x: float) -> str:
    return f"{x:g}".replace(".", ",")  # Spanish decimal comma


def _pct(x: float) -> str:
    return f"{100 * x:.1f}".replace(".", ",") + "%"


def _trend_text(trend: str) -> str | None:
    return {"with": "solo a favor de la tendencia diaria (media de 50 días)",
            "against": "solo contra la tendencia diaria (media de 50 días)"}.get(trend)


def _target_text(target: str) -> str:
    return {"close": "sin objetivo (cierra al final del día)", "fill": "objetivo: que se cierre el hueco (cierre de ayer)",
            "vwap": "objetivo: volver al VWAP"}.get(target, f"objetivo {target}")


@dataclass(frozen=True)
class ORBRule:
    family: ClassVar[str] = "orb"
    range_min: int = 15
    direction: str = "both"
    confirm: str = "close"
    stop: str = "range"
    target_r: float = 0.0
    cutoff_min: int = 120
    min_range_atr: float = 0.0
    max_range_atr: float = 99.0
    min_rel_vol: float = 0.0
    gap_min: float = -1.0
    gap_max: float = 1.0
    trend: str = "any"

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def version_id(self) -> str:
        return hash_obj({"family": "orb", **self.to_dict()}, 24)

    def complexity(self) -> int:
        """Active filters + a target: every extra knob is one more way to fit the past."""
        return sum(getattr(self, k) != v for k, v in FILTERS_OFF.items()) + (self.target_r > 0)

    def valid(self) -> bool:
        return self.gap_min < self.gap_max and self.min_range_atr < self.max_range_atr

    def canonical(self) -> "ORBRule":
        return self

    def shape(self) -> tuple:
        return ("orb", self.range_min, self.direction, self.confirm, self.stop)

    def describe(self) -> str:
        side = {"long": "compra si rompe el máximo", "short": "vende en corto si rompe el mínimo",
                "both": "compra si rompe el máximo / corto si rompe el mínimo"}[self.direction]
        how = "con una vela que cierra fuera del rango" if self.confirm == "close" else "en cuanto lo toca (orden stop)"
        parts = [f"Rango de los primeros {self.range_min} min; {side} {how}",
                 "stop en el otro extremo del rango" if self.stop == "range" else "stop en la mitad del rango",
                 f"objetivo {_n(self.target_r)}R" if self.target_r else "sin objetivo (cierra al final del día)",
                 f"entradas durante los {self.cutoff_min} min siguientes al rango"]
        if self.min_range_atr > 0 or self.max_range_atr < 99:
            lo = f"≥ {_n(self.min_range_atr)}" if self.min_range_atr > 0 else ""
            hi = f"≤ {_n(self.max_range_atr)}" if self.max_range_atr < 99 else ""
            parts.append(f"tamaño del rango {' y '.join(x for x in (lo, hi) if x)} × ATR diario")
        if self.min_rel_vol > 0:
            parts.append(f"volumen del rango ≥ {_n(self.min_rel_vol)}× su media de 20 días")
        if self.gap_min > -1 or self.gap_max < 1:
            lo = f"{100 * self.gap_min:+.1f}%" if self.gap_min > -1 else "-∞"
            hi = f"{100 * self.gap_max:+.1f}%" if self.gap_max < 1 else "+∞"
            parts.append(f"hueco de apertura entre {lo} y {hi}")
        if _trend_text(self.trend):
            parts.append(_trend_text(self.trend))
        return "; ".join(parts)


@dataclass(frozen=True)
class GapRule:
    """Opening gap (today's open vs yesterday's close): bet that it closes (`fade`) or that it continues (`go`).
    The gap is only known once the market has opened, so the entry is at a later bar's open (never the open itself)."""
    family: ClassVar[str] = "gap"
    mode: str = "fade"
    min_gap: float = 0.01      # size of the gap, at least ...
    max_gap: float = 1.0       # ... and at most (1 = no limit)
    side: str = "both"         # which gaps: up | down | both
    entry_min: int = 15        # minutes after the open
    confirm: bool = False      # the price must already be moving the expected way at the entry
    stop_atr: float = 0.5      # stop distance in daily ATRs
    target: str = "close"      # close | fill (back to yesterday's close; fade only) | 1R | 2R
    trend: str = "any"

    def to_dict(self) -> dict:
        return {"family": "gap", **asdict(self)}

    @property
    def version_id(self) -> str:
        return hash_obj(self.to_dict(), 24)

    def complexity(self) -> int:
        return (self.max_gap < 1) + self.confirm + (self.target != "close") + (self.trend != "any") + (self.side != "both")

    def valid(self) -> bool:
        return self.min_gap < self.max_gap and not (self.target == "fill" and self.mode != "fade")

    def canonical(self) -> "GapRule":
        return self

    def shape(self) -> tuple:
        return ("gap", self.mode, self.side, self.entry_min)

    def describe(self) -> str:
        size = f"de al menos {_pct(self.min_gap)}" + (f" y como mucho {_pct(self.max_gap)}" if self.max_gap < 1 else "")
        which = {"both": "", "up": " al alza", "down": " a la baja"}[self.side]
        act = {("fade", "up"): "vende en corto", ("fade", "down"): "compra", ("go", "up"): "compra",
               ("go", "down"): "vende en corto"}
        if self.side == "both":
            how = ("vende en corto si abre al alza, compra si abre a la baja" if self.mode == "fade"
                   else "compra si abre al alza, vende en corto si abre a la baja")
        else:
            how = act[(self.mode, self.side)]
        bet = f"apuesta a que {'se cierra' if self.mode == 'fade' else 'continúa'}: {how}"
        parts = [f"Hueco de apertura{which} {size}: {bet}",
                 f"entra a los {self.entry_min} min de la apertura"
                 + (" si el precio ya va en esa dirección" if self.confirm else ""),
                 f"stop a {_n(self.stop_atr)} ATR diarios", _target_text(self.target)]
        if _trend_text(self.trend):
            parts.append(_trend_text(self.trend))
        return "; ".join(parts)


@dataclass(frozen=True)
class VWAPRule:
    """VWAP = average price of the day weighted by volume, from the open up to each bar. `cross`: follow the price
    when a bar closes on the other side of the VWAP; `revert`: when the price is stretched far from the VWAP, bet
    that it comes back."""
    family: ClassVar[str] = "vwap"
    mode: str = "cross"
    direction: str = "both"
    start_min: int = 30        # no signals before this minute
    window_min: int = 120      # signals only during this many minutes after `start_min`
    stretch_atr: float = 0.5   # revert: distance from the VWAP in daily ATRs
    stop_atr: float = 0.5
    target: str = "close"      # close | vwap (revert only) | 1R | 2R
    trend: str = "any"

    def to_dict(self) -> dict:
        return {"family": "vwap", **asdict(self)}

    @property
    def version_id(self) -> str:
        return hash_obj(self.to_dict(), 24)

    def complexity(self) -> int:
        return (self.target != "close") + (self.trend != "any")

    def valid(self) -> bool:
        return not (self.target == "vwap" and self.mode != "revert")

    def canonical(self) -> "VWAPRule":
        return replace(self, stretch_atr=0.5) if self.mode == "cross" else self  # unused by `cross`

    def shape(self) -> tuple:
        return ("vwap", self.mode, self.direction, self.start_min)

    def describe(self) -> str:
        side = {"long": "solo largos", "short": "solo cortos", "both": "largos y cortos"}[self.direction]
        if self.mode == "cross":
            what = ("VWAP (precio medio del día ponderado por volumen): compra cuando una vela cierra por encima tras "
                    "estar por debajo, corto al revés")
        else:
            what = (f"VWAP (precio medio del día ponderado por volumen): si el precio se aleja {_n(self.stretch_atr)} ATR "
                    "diarios del VWAP, apuesta a que vuelve (compra por debajo, corto por encima)")
        parts = [f"{what} ({side})", f"señales desde el minuto {self.start_min} y durante {self.window_min} min",
                 f"entra en la vela siguiente; stop a {_n(self.stop_atr)} ATR diarios", _target_text(self.target)]
        if _trend_text(self.trend):
            parts.append(_trend_text(self.trend))
        return "; ".join(parts)


Rule = ORBRule | GapRule | VWAPRule
FAMILIES = {"orb": ORBRule, "gap": GapRule, "vwap": VWAPRule}


def rule_from_dict(d: dict) -> Rule:
    cls = FAMILIES[d.get("family", "orb")]
    return cls(**{f.name: _py(d[f.name]) for f in fields(cls) if f.name in d})


def family_space(family: str, dataset: str) -> dict:
    if family == "orb":
        return {**COMMON, **SPACE[dataset]}
    if family == "gap":
        return {**GAP_SPACE, "entry_min": GAP_ENTRY[dataset]}
    return {**VWAP_SPACE, **VWAP_TIME[dataset]}


# ---------------------------------------------------------------------- data preparation
@dataclass
class SymbolDays:
    """One stock's sessions as matrices (sessions x bars of the day), NaN where a bar is missing."""
    sessions: pd.DatetimeIndex
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray
    last_col: np.ndarray  # column of the session's last bar
    gap: np.ndarray       # open vs the previous session's last close (NaN if that session is not stored)
    atr_pct: np.ndarray   # previous session's 14-day ATR / close
    trend: np.ndarray     # previous session's close / 50-day mean - 1

    def subset(self, keep: np.ndarray) -> "SymbolDays":
        return SymbolDays(self.sessions[keep], self.o[keep], self.h[keep], self.l[keep], self.c[keep], self.v[keep],
                          self.last_col[keep], self.gap[keep], self.atr_pct[keep], self.trend[keep])

    def last_close(self) -> pd.Series:
        return pd.Series(self.c[np.arange(len(self.sessions)), self.last_col], index=self.sessions)


def session_dates(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """NYSE session date (00:00 UTC, like the schedule index) of each intraday timestamp."""
    return idx.tz_convert("America/New_York").tz_localize(None).normalize().tz_localize("UTC")


def prepare_symbol(bars: pd.DataFrame, daily: pd.DataFrame | None, bar_min: int) -> SymbolDays | None:
    """`bars`: intraday bars (UTC index); `daily`: daily research frame with `available_at`."""
    if bars is None or len(bars) == 0:
        return None
    idx = pd.DatetimeIndex(bars.index).tz_convert("UTC")
    day = session_dates(idx)
    sched = nyse_schedule(day.min(), day.max())
    opens = sched["market_open"].reindex(day).values  # NaT outside NYSE sessions
    offset = (idx.tz_localize(None).values - opens) / np.timedelta64(1, "m")
    ok = np.isfinite(offset) & (offset >= 0)
    if not ok.any():
        return None
    slot = (offset[ok] // bar_min).astype(int)
    sessions = pd.DatetimeIndex(np.unique(day[ok].tz_localize(None).values)).tz_localize("UTC")
    row = sessions.get_indexer(day[ok])
    width = int(slot.max()) + 1
    mats = {}
    for col in ("open", "high", "low", "close", "volume"):
        mtx = np.full((len(sessions), width), np.nan)
        mtx[row, slot] = bars[col].to_numpy(dtype=float)[ok]
        mats[col] = mtx
    has = ~np.isnan(mats["close"])
    last_col = width - 1 - np.argmax(has[:, ::-1], axis=1)
    # a session downloaded while the market was open (or missing its last bars) is not a whole day: dropped
    length = (sched["market_close"] - sched["market_open"]).reindex(sessions).dt.total_seconds().to_numpy() / 60
    whole = (last_col + 1) * bar_min >= length - 15
    if not whole.all():
        sessions, last_col = sessions[whole], last_col[whole]
        mats = {k: v[whole] for k, v in mats.items()}
        if len(sessions) == 0:
            return None
    last_close = mats["close"][np.arange(len(sessions)), last_col]
    pos = sched.index.get_indexer(sessions)
    consecutive = np.r_[False, np.diff(pos) == 1]
    prev_close = np.r_[np.nan, last_close[:-1]]
    gap = np.where(consecutive, mats["open"][:, 0] / prev_close - 1, np.nan)
    atr_pct = np.full(len(sessions), np.nan)
    trend = np.full(len(sessions), np.nan)
    if daily is not None and len(daily):
        d = daily[["high", "low", "close", "available_at"]].copy()
        prev = d["close"].shift()
        tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(), (d["low"] - prev).abs()], axis=1).max(axis=1)
        d["atr_pct"] = tr.rolling(14, min_periods=14).mean() / d["close"]
        d["trend"] = d["close"] / d["close"].rolling(50, min_periods=50).mean() - 1
        open_times = pd.Series(sched["market_open"].reindex(sessions).values, index=sessions)
        ctx = align_higher_timeframe(open_times, d, ["atr_pct", "trend"], "")  # previous session's values only
        atr_pct, trend = ctx["atr_pct"].to_numpy(float), ctx["trend"].to_numpy(float)
    return SymbolDays(sessions, mats["open"], mats["high"], mats["low"], mats["close"], mats["volume"], last_col,
                      gap, atr_pct, trend)


# ---------------------------------------------------------------------- simulation of one stock
def _first(mask: np.ndarray) -> np.ndarray:
    """Column of the first True per row, -1 if none."""
    return np.where(mask.any(axis=1), mask.argmax(axis=1), -1)


def _no_trades() -> pd.DataFrame:
    return pd.DataFrame({"session": pd.DatetimeIndex([], tz="UTC"), "entry_min": np.array([], int),
                         "direction": np.array([], float), "net": np.array([], float),
                         "risk_frac": np.array([], float), "r": np.array([], float), "exit": np.array([], int)})


@dataclass
class Entries:
    """Planned trades of one stock (one per session at most), before the exits are simulated."""
    rows: np.ndarray       # session rows
    dirn: np.ndarray       # +1 long / -1 short
    e_bar: np.ndarray      # entry bar
    entry: np.ndarray      # entry price
    stop: np.ndarray
    target: np.ndarray     # NaN = none (flat at the close)
    scan_from: np.ndarray  # first bar where the stop / target can be hit
    stop_order: bool = False  # entered by a stop order inside e_bar: if that bar also reached the stop, stopped


def simulate_symbol(sd: SymbolDays, rule: Rule, bar_min: int, cost_bps: float) -> pd.DataFrame:
    """One row per trade: session, entry minute, direction (+1/-1), net return on the position (after costs on
    both sides), risk as a fraction of the entry price, result in R, exit kind (0 close, 1 stop, 2 target)."""
    if len(sd.sessions) == 0 or sd.c.shape[1] < 3:
        return _no_trades()
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        en = {"orb": _orb_entries, "gap": _gap_entries, "vwap": _vwap_entries}[rule.family](sd, rule, bar_min)
        return _no_trades() if en is None or len(en.rows) == 0 else _exits(sd, en, bar_min, cost_bps)


def _trend_ok(sd: SymbolDays, trend: str, dirn: np.ndarray, rows: np.ndarray) -> np.ndarray:
    if trend == "any":
        return np.ones(len(rows), bool)
    t = sd.trend[rows] * dirn
    return t > 0 if trend == "with" else t < 0


def _orb_entries(sd: SymbolDays, rule: ORBRule, bar_min: int) -> Entries | None:
    B = sd.c.shape[1]
    k = max(1, rule.range_min // bar_min)
    if B <= k + 1:
        return None
    orh, orl = np.nanmax(sd.h[:, :k], axis=1), np.nanmin(sd.l[:, :k], axis=1)
    ok = np.all(~np.isnan(sd.c[:, :k]), axis=1) & (sd.last_col > k) & (orh > orl)
    if rule.min_range_atr > 0 or rule.max_range_atr < 99:
        ratio = (orh - orl) / sd.o[:, 0] / sd.atr_pct
        ok &= np.isfinite(ratio) & (ratio >= rule.min_range_atr) & (ratio <= rule.max_range_atr)
    if rule.min_rel_vol > 0:
        orv = np.nansum(sd.v[:, :k], axis=1)
        rel_vol = orv / pd.Series(orv).rolling(20, min_periods=10).mean().shift(1).to_numpy()  # previous sessions
        ok &= np.isfinite(rel_vol) & (rel_vol >= rule.min_rel_vol)
    if rule.gap_min > -1 or rule.gap_max < 1:
        ok &= np.isfinite(sd.gap) & (sd.gap >= rule.gap_min) & (sd.gap <= rule.gap_max)
    up_ok, dn_ok = ok, ok
    if rule.trend == "with":
        up_ok, dn_ok = ok & (sd.trend > 0), ok & (sd.trend < 0)
    elif rule.trend == "against":
        up_ok, dn_ok = ok & (sd.trend < 0), ok & (sd.trend > 0)
    cols = np.arange(B)[None, :]
    last = sd.last_col[:, None]
    window = (cols >= k) & (cols < k + max(1, rule.cutoff_min // bar_min)) & (cols <= last)
    if rule.confirm == "close":
        window &= cols < last  # the entry is at the NEXT bar's open
        up, dn = sd.c > orh[:, None], sd.c < orl[:, None]
    else:
        up, dn = sd.h >= orh[:, None], sd.l <= orl[:, None]
    up = up & window & up_ok[:, None] & (rule.direction != "short")
    dn = dn & window & dn_ok[:, None] & (rule.direction != "long")
    ju, jd = _first(up), _first(dn)
    long_ = (ju >= 0) & ((jd < 0) | (ju < jd))  # both edges in the same bar: neither (order unknown)
    short = (jd >= 0) & ((ju < 0) | (jd < ju))
    rows = np.flatnonzero(long_ | short)
    dirn = np.where(long_[rows], 1.0, -1.0)
    t = np.where(long_[rows], ju[rows], jd[rows])
    hi_r, lo_r = orh[rows], orl[rows]
    if rule.confirm == "close":
        e_bar, scan_from = t + 1, t + 1  # the whole entry bar happens after the entry
        entry = sd.o[rows, e_bar]
    else:  # stop order at the edge: filled there, or at the bar's open if it gaps through
        e_bar, scan_from = t, t + 1
        bar_open = sd.o[rows, t]
        entry = np.where(dirn > 0, np.maximum(bar_open, hi_r), np.minimum(bar_open, lo_r))
    stop = np.where(dirn > 0, lo_r, hi_r) if rule.stop == "range" else (hi_r + lo_r) / 2
    target = entry + dirn * rule.target_r * dirn * (entry - stop) if rule.target_r > 0 else np.full(len(rows), np.nan)
    return Entries(rows, dirn, e_bar, entry, stop, target, scan_from, stop_order=rule.confirm == "touch")


def _r_target(entry, dirn, stop, target: str) -> np.ndarray:
    """Profit target `nR` = n times the risk (entry to stop) beyond the entry; NaN = none."""
    r = {"1R": 1.0, "2R": 2.0}.get(target)
    return entry + dirn * r * dirn * (entry - stop) if r else np.full(len(entry), np.nan)


def _gap_entries(sd: SymbolDays, rule: GapRule, bar_min: int) -> Entries | None:
    e = max(1, rule.entry_min // bar_min)  # the gap is known at the open: enter at a later bar's open
    if sd.c.shape[1] <= e + 1:
        return None
    g = sd.gap
    size = np.abs(g)
    ok = np.isfinite(g) & (size >= rule.min_gap) & (size <= rule.max_gap) & (sd.last_col > e) & np.isfinite(sd.atr_pct)
    ok &= {"both": g != 0, "up": g > 0, "down": g < 0}[rule.side]
    up = np.sign(g)
    dirn_all = -up if rule.mode == "fade" else up
    if rule.confirm:  # by the entry the price already moves the way of the bet
        ok &= dirn_all * (sd.c[:, e - 1] - sd.o[:, 0]) > 0
    rows = np.flatnonzero(ok)
    dirn = dirn_all[rows]
    keep = _trend_ok(sd, rule.trend, dirn, rows)
    rows, dirn = rows[keep], dirn[keep]
    entry = sd.o[rows, e]
    stop = entry - dirn * rule.stop_atr * sd.atr_pct[rows] * entry
    if rule.target == "fill":
        target = sd.o[rows, 0] / (1 + g[rows])  # yesterday's close
        keep = dirn * (target - entry) > 0  # the gap already closed before the entry: no trade
        rows, dirn, entry, stop, target = rows[keep], dirn[keep], entry[keep], stop[keep], target[keep]
    else:
        target = _r_target(entry, dirn, stop, rule.target)
    eb = np.full(len(rows), e)
    return Entries(rows, dirn, eb, entry, stop, target, eb.copy())


def _vwap_entries(sd: SymbolDays, rule: VWAPRule, bar_min: int) -> Entries | None:
    B = sd.c.shape[1]
    s0 = max(1, rule.start_min // bar_min)
    if B <= s0 + 1:
        return None
    tp = (sd.h + sd.l + sd.c) / 3
    vol = np.nan_to_num(sd.v)
    cum_v = np.cumsum(vol, axis=1)
    vwap = np.cumsum(np.nan_to_num(tp) * vol, axis=1) / np.where(cum_v > 0, cum_v, np.nan)  # bars up to t only
    cols = np.arange(B)[None, :]
    last = sd.last_col[:, None]
    window = (cols >= s0) & (cols < s0 + max(1, rule.window_min // bar_min)) & (cols < last)  # entry at t + 1
    window &= np.isfinite(sd.atr_pct)[:, None]
    if rule.mode == "cross":
        prev_c = np.c_[np.full(len(sd.c), np.nan), sd.c[:, :-1]]
        prev_v = np.c_[np.full(len(sd.c), np.nan), vwap[:, :-1]]
        up = (sd.c > vwap) & (prev_c <= prev_v)
        dn = (sd.c < vwap) & (prev_c >= prev_v)
    else:
        dist = (sd.c - vwap) / (sd.atr_pct[:, None] * sd.o[:, [0]])
        up, dn = dist <= -rule.stretch_atr, dist >= rule.stretch_atr  # stretched below: buy; above: short
    up = up & window & (rule.direction != "short")
    dn = dn & window & (rule.direction != "long")
    ju, jd = _first(up), _first(dn)
    long_ = (ju >= 0) & ((jd < 0) | (ju < jd))
    short = (jd >= 0) & ((ju < 0) | (jd < ju))
    rows = np.flatnonzero(long_ | short)
    dirn = np.where(long_[rows], 1.0, -1.0)
    t = np.where(long_[rows], ju[rows], jd[rows])
    keep = _trend_ok(sd, rule.trend, dirn, rows)
    rows, dirn, t = rows[keep], dirn[keep], t[keep]
    e_bar = t + 1
    entry = sd.o[rows, e_bar]
    stop = entry - dirn * rule.stop_atr * sd.atr_pct[rows] * entry
    if rule.target == "vwap":
        target = vwap[rows, t]  # the VWAP when the signal appeared
        target = np.where(dirn * (target - entry) > 0, target, np.nan)
        keep = np.isfinite(target)
        rows, dirn, e_bar, entry, stop, target = rows[keep], dirn[keep], e_bar[keep], entry[keep], stop[keep], target[keep]
    else:
        target = _r_target(entry, dirn, stop, rule.target)
    return Entries(rows, dirn, e_bar, entry, stop, target, e_bar.copy())


def _exits(sd: SymbolDays, en: Entries, bar_min: int, cost_bps: float) -> pd.DataFrame:
    """Stop, target or the session's last bar, whichever comes first (conservative fills)."""
    B = sd.c.shape[1]
    rows, dirn, e_bar, entry, stop, target = en.rows, en.dirn, en.e_bar, en.entry, en.stop, en.target
    n = np.arange(len(rows))
    hh, ll, oo, cc = sd.h[rows], sd.l[rows], sd.o[rows], sd.c[rows]
    lastc = sd.last_col[rows]
    cols = np.arange(B)[None, :]
    risk = dirn * (entry - stop)
    after = (cols >= en.scan_from[:, None]) & (cols <= lastc[:, None])
    longs = dirn[:, None] > 0
    stop_hit = after & np.where(longs, ll <= stop[:, None], hh >= stop[:, None])
    has_t = np.isfinite(target)
    tgt_hit = after & has_t[:, None] & np.where(longs, hh >= target[:, None], ll <= target[:, None])
    js, jt = _first(stop_hit), _first(tgt_hit)
    by_stop = (js >= 0) & ((jt < 0) | (js <= jt))  # same bar: the stop is assumed to come first
    by_tgt = (jt >= 0) & ~by_stop
    o_s, o_t = oo[n, np.maximum(js, 0)], oo[n, np.maximum(jt, 0)]
    stop_px = np.where(dirn > 0, np.minimum(o_s, stop), np.maximum(o_s, stop))  # a gap through the stop fills worse
    tgt_px = np.where(dirn > 0, np.maximum(o_t, target), np.minimum(o_t, target))
    exit_px = np.where(by_stop, stop_px, np.where(by_tgt, tgt_px, cc[n, lastc]))
    kind = np.where(by_stop, 1, np.where(by_tgt, 2, 0))
    if en.stop_order:  # the entry bar itself also reached the stop: stopped (order inside the bar unknown)
        same = np.where(dirn > 0, ll[n, e_bar] <= stop, hh[n, e_bar] >= stop)
        exit_px, kind = np.where(same, stop, exit_px), np.where(same, 1, kind)
    net = dirn * (exit_px - entry) / entry - 2 * cost_bps / 1e4
    good = np.isfinite(entry) & (risk > 0) & np.isfinite(net)
    out = pd.DataFrame({"session": sd.sessions[rows], "entry_min": e_bar * bar_min, "direction": dirn, "net": net,
                        "risk_frac": risk / entry, "r": dirn * (exit_px - entry) / risk, "exit": kind})
    return out[good].reset_index(drop=True)


# ---------------------------------------------------------------------- portfolio of all stocks
@dataclass
class PortfolioConfig:
    risk_per_trade: float = 0.005   # fraction of equity lost if the stop is hit (before costs/gaps)
    max_positions: int = 10         # trades per day (first come); each at most equity / max_positions: no leverage
    cost_bps: float = 10.0          # per side: spread + slippage (intraday fills are rougher than daily ones)
    bars_per_year: int = 252


def _tie(symbol: str, sessions: pd.Series) -> np.ndarray:
    """Deterministic pseudo-random order of simultaneous entries (differs per day)."""
    days = pd.DatetimeIndex(sessions).tz_localize(None).values.astype("datetime64[D]").astype(np.int64).astype(np.uint64)
    return (days * np.uint64(2654435761) ^ np.uint64(zlib.crc32(symbol.encode()))) & np.uint64(0xFFFFFFFF)


def portfolio_returns(trades: dict[str, pd.DataFrame], sessions: pd.DatetimeIndex,
                      pc: PortfolioConfig) -> tuple[pd.Series, pd.DataFrame]:
    """Daily returns (0 on days without trades) and the trades actually taken (the first `max_positions` entries
    of each day, by entry time)."""
    frames = [t.assign(symbol=s, tie=_tie(s, t["session"])) for s, t in trades.items() if len(t)]
    if not frames:
        return pd.Series(0.0, index=sessions), _no_trades().assign(symbol=pd.Series([], dtype=str),
                                                                   weight=np.array([], float))
    tr = pd.concat(frames, ignore_index=True).sort_values(["session", "entry_min", "tie"], kind="stable")
    tr = tr[tr.groupby("session").cumcount() < pc.max_positions]
    w = np.minimum(pc.risk_per_trade / tr["risk_frac"].to_numpy(), 1.0 / pc.max_positions)
    tr = tr.assign(weight=w).drop(columns="tie").reset_index(drop=True)
    daily = (tr["weight"] * tr["net"]).groupby(tr["session"]).sum().reindex(sessions, fill_value=0.0)
    return daily, tr


def _sharpe(r: pd.Series, bpy: int) -> float:
    sd = r.std(ddof=1)
    return float(r.mean() / sd * np.sqrt(bpy)) if len(r) > 1 and sd > 0 else 0.0


def summarize(daily: pd.Series, tr: pd.DataFrame, pc: PortfolioConfig, blocks: int = 3) -> dict:
    eq = (1 + daily).cumprod()
    out = {"total_return": float(eq.iloc[-1] - 1) if len(eq) else 0.0, "sharpe": _sharpe(daily, pc.bars_per_year),
           "max_drawdown": float((eq / eq.cummax() - 1).min()) if len(eq) else 0.0, "n_trades": int(len(tr)),
           "win_rate": float((tr["net"] > 0).mean()) if len(tr) else None,
           "avg_trade": float(tr["net"].mean()) if len(tr) else None,
           "short_share": float((tr["direction"] < 0).mean()) if len(tr) else None,
           "days_traded": float((daily != 0).mean()) if len(daily) else 0.0, "sessions": int(len(daily))}
    bl = []
    for b in np.array_split(np.arange(len(daily)), blocks):
        if len(b) == 0:
            continue
        r = daily.iloc[b]
        n = int(tr["session"].between(r.index[0], r.index[-1]).sum()) if len(tr) else 0
        bl.append({"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sharpe": _sharpe(r, pc.bars_per_year),
                   "return": float((1 + r).prod() - 1), "trades": n})
    out["blocks"] = bl
    out |= return_stats(daily) or {}
    return out


def passive_returns(days: dict[str, SymbolDays], sessions: pd.DatetimeIndex, symbols=None) -> pd.Series:
    """Holding the same stocks, equal weight, close to close (a day after a gap in the stored data counts 0)."""
    rets = []
    for s, d in days.items():
        if (symbols is not None and s not in symbols) or len(d.sessions) == 0:
            continue
        lc = d.last_close()
        pos = nyse_schedule(lc.index.min(), lc.index.max()).index.get_indexer(lc.index)
        r = lc.pct_change().where(np.r_[False, np.diff(pos) == 1])
        rets.append(r.rename(s))
    if not rets:
        return pd.Series(0.0, index=sessions)
    return pd.concat(rets, axis=1).reindex(sessions).mean(axis=1, skipna=True).fillna(0.0)


def passive_summary(r: pd.Series, bpy: int) -> dict:
    eq = (1 + r).cumprod()
    return {"total_return": float(eq.iloc[-1] - 1) if len(eq) else 0.0, "sharpe": _sharpe(r, bpy),
            "max_drawdown": float((eq / eq.cummax() - 1).min()) if len(eq) else 0.0}


def split_halves(symbols, seed: int = 7) -> list[list[str]]:
    syms = sorted(symbols)
    order = [syms[i] for i in np.random.default_rng(seed).permutation(len(syms))]
    return [sorted(order[: len(order) // 2]), sorted(order[len(order) // 2:])]


# ---------------------------------------------------------------------- the out-of-sample boundary
def _current_boundary(sf, dataset: str):
    with sf() as s:
        return s.scalars(select(m.IntradayBoundary).where(m.IntradayBoundary.dataset == dataset)
                         .order_by(m.IntradayBoundary.epoch.desc()).limit(1)).first()


def dataset_boundary(sf, dataset: str, sessions: pd.DatetimeIndex) -> tuple[int, pd.Timestamp]:
    """(epoch, first out-of-sample session). Set once per dataset; a later epoch only when the stored sessions have
    at least doubled since the current boundary was set (its sessions are then fresh, never-seen data)."""
    n = len(sessions)
    cut = sessions[min(n - 1, int(n * (1 - OOS_SHARE)))]
    for _ in range(3):
        cur = _current_boundary(sf, dataset)
        if cur is not None and not (n >= 2 * cur.n_sessions and cut.date() > cur.oos_start):
            return cur.epoch, pd.Timestamp(cur.oos_start, tz="UTC")
        try:
            with sf() as s, s.begin():
                s.add(m.IntradayBoundary(dataset=dataset, epoch=(cur.epoch + 1) if cur else 1, oos_start=cut.date(),
                                         n_sessions=n))
        except IntegrityError:  # set at the same moment by another thread: read it again
            continue
    cur = _current_boundary(sf, dataset)
    return cur.epoch, pd.Timestamp(cur.oos_start, tz="UTC")


def boundaries(sf) -> list[dict]:
    with sf() as s:
        rows = s.scalars(select(m.IntradayBoundary).order_by(m.IntradayBoundary.dataset, m.IntradayBoundary.epoch)).all()
    return [{"dataset": r.dataset, "epoch": r.epoch, "oos_start": str(r.oos_start), "n_sessions": r.n_sessions,
             "set_at": r.set_at.isoformat(timespec="seconds") if r.set_at else None} for r in rows]


# ---------------------------------------------------------------------- the lab
class StopRequested(Exception):
    pass


class IntradayLab:
    def __init__(self, sf, dataset: str, bars: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame],
                 pc: PortfolioConfig | None = None, log: Callable[[str], None] | None = None,
                 stop_event: threading.Event | None = None, seed: int | None = None, complexity_penalty: float = 0.05,
                 families: tuple[str, ...] = tuple(FAMILIES)):
        if dataset not in DATASETS:
            raise ValueError(f"tipo de velas desconocido: {dataset}")
        self.sf, self.dataset = sf, dataset
        self.bar_min, self.pc = BAR_MIN[dataset], pc or PortfolioConfig()
        self.log = log or (lambda msg: None)
        self.stop_event = stop_event or threading.Event()
        self.rng = np.random.default_rng(seed)
        self.penalty = complexity_penalty
        self.families = tuple(f for f in families if f in FAMILIES) or tuple(FAMILIES)  # what the search tries
        prepared = {s: d for s, b in sorted(bars.items())
                    if (d := prepare_symbol(b, daily.get(s), self.bar_min)) is not None}
        if not prepared:
            raise ValueError("no hay velas intradía: descárgalas primero")
        self.sessions = pd.DatetimeIndex(sorted(set().union(*[set(d.sessions) for d in prepared.values()])))
        if len(self.sessions) < 20:
            raise ValueError(f"muy pocas sesiones intradía ({len(self.sessions)}); hacen falta al menos 20")
        self.epoch, self.oos_start = dataset_boundary(sf, dataset, self.sessions)
        self._all_days = prepared
        # the search only ever sees the research part
        self.days = {s: d.subset(d.sessions < self.oos_start) for s, d in prepared.items()}
        self.research_sessions = self.sessions[self.sessions < self.oos_start]
        self.oos_sessions = self.sessions[self.sessions >= self.oos_start]
        n_pre = max(1, int(len(self.research_sessions) * PRE_EXAM_SHARE))
        self.search_sessions, self.pre_sessions = self.research_sessions[:-n_pre], self.research_sessions[-n_pre:]
        if len(self.search_sessions) < 6:
            raise ValueError("muy pocas sesiones para buscar")
        self.halves = split_halves(self.days) if len(self.days) >= 4 else []
        self.can_validate = len(self.search_sessions) >= MIN_SEARCH_SESSIONS
        self.universe_id = hash_obj({"dataset": dataset, "symbols": sorted(self.days), "epoch": self.epoch,
                                     "oos_start": str(self.oos_start.date()), "pc": asdict(self.pc),
                                     "penalty": self.penalty, "first": str(self.sessions[0].date())}, 32)
        self.session_trials = 0
        self.phase = "parado"
        self._cache: dict[str, dict[str, pd.DataFrame]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ evaluation
    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRequested()

    def trades(self, rule: Rule, cost_bps: float | None = None, oos: bool = False) -> dict[str, pd.DataFrame]:
        cb = self.pc.cost_bps if cost_bps is None else cost_bps
        key = f"{rule.version_id}|{cb}|{oos}"
        with self._cache_lock:
            hit = self._cache.get(key)
        if hit is None:
            days = self._all_days if oos else self.days
            hit = {s: simulate_symbol(d, rule, self.bar_min, cb) for s, d in days.items()}
            with self._cache_lock:
                if len(self._cache) > 48:
                    self._cache.clear()
                self._cache[key] = hit
        return hit

    def window(self, rule: Rule, sessions: pd.DatetimeIndex, symbols=None, cost_bps: float | None = None,
               oos: bool = False):
        allt = self.trades(rule, cost_bps, oos)
        sel = {s: t[t["session"].isin(sessions)] for s, t in allt.items() if symbols is None or s in symbols}
        return portfolio_returns(sel, sessions, self.pc)

    def score(self, rule: Rule) -> tuple[float | None, dict]:
        """Worst block Sharpe of the search window, for all stocks and for each half, minus complexity."""
        daily, tr = self.window(rule, self.search_sessions)
        mt = summarize(daily, tr, self.pc)
        mt["complexity"] = rule.complexity()
        if mt["n_trades"] < 30:
            mt["invalid_reason"] = f"pocas operaciones ({mt['n_trades']} < 30)"
            return None, _clean(mt)
        if any(b["trades"] < 5 for b in mt["blocks"]):
            mt["invalid_reason"] = "casi sin operaciones en algún tramo"
            return None, _clean(mt)
        worst = [b["sharpe"] for b in mt["blocks"]]
        halves = []
        for name, h in zip("AB", self.halves):
            hd, ht = self.window(rule, self.search_sessions, set(h))
            hm = summarize(hd, ht, self.pc)
            halves.append({"name": name, "consistency": min(b["sharpe"] for b in hm["blocks"]), "sharpe": hm["sharpe"],
                           "n_trades": hm["n_trades"]})
            worst += [b["sharpe"] for b in hm["blocks"]]
        mt["halves"] = halves
        return float(min(worst) - self.penalty * rule.complexity()), _clean(mt)

    def _row_id(self, rule: Rule) -> str:
        return hash_obj({"rule": rule.version_id, "universe": self.universe_id}, 32)

    def evaluate_and_store(self, rule: Rule, origin: str, cycle: int) -> float | None:
        rid = self._row_id(rule)
        with self.sf() as s:
            row = s.get(m.IntradayCandidate, rid)
            if row is not None:
                return row.fitness
        self.check_stop()
        fit, mt = self.score(rule)
        with self.sf() as s, s.begin():
            s.add(m.IntradayCandidate(id=rid, dataset=self.dataset, universe_id=self.universe_id, family=rule.family,
                                      version_id=rule.version_id, rule=rule.to_dict(), origin=origin, cycle=cycle,
                                      fitness=fit, sr=mt.get("sr"), metrics=mt,
                                      status="EVALUATED" if fit is not None else "INVALID"))
        self.session_trials += 1
        return fit

    # ------------------------------------------------------------------ search
    def random_rule(self) -> Rule:
        family = self.families[self.rng.integers(len(self.families))]
        space, cls = family_space(family, self.dataset), FAMILIES[family]
        while True:
            d = {k: _py(v[self.rng.integers(len(v))]) for k, v in space.items()}
            for k, off in OPTIONAL[family].items():  # extras are optional: new rules start with few of them
                if self.rng.random() < 0.6:
                    d[k] = off
            r = cls(**d).canonical()
            if r.valid():
                return r

    def mutate(self, rule: Rule) -> Rule:
        space = family_space(rule.family, self.dataset)
        keys = list(space)
        for _ in range(30):
            k = keys[self.rng.integers(len(keys))]
            r = replace(rule, **{k: _py(space[k][self.rng.integers(len(space[k]))])}).canonical()
            if r.valid() and r != rule:
                return r
        return self.random_rule()

    def top_rows(self, n: int, positive: bool = False, family: str | None = None) -> list:
        with self.sf() as s:
            q = select(m.IntradayCandidate).where(m.IntradayCandidate.universe_id == self.universe_id,
                                                  m.IntradayCandidate.fitness.is_not(None))
            if positive:
                q = q.where(m.IntradayCandidate.fitness > 0)
            if family == "orb":
                q = q.where(or_(m.IntradayCandidate.family == "orb", m.IntradayCandidate.family.is_(None)))
            elif family:
                q = q.where(m.IntradayCandidate.family == family)
            return list(s.scalars(q.order_by(m.IntradayCandidate.fitness.desc()).limit(n)))

    def elites(self, n: int = 8) -> list[Rule]:
        """Best rules, at most two per basic shape (family and its main settings), so the search does not get stuck."""
        out, seen = [], {}
        for r in self.top_rows(60):
            rule = rule_from_dict(r.rule)
            if rule.family not in self.families:
                continue
            shape = rule.shape()
            if seen.get(shape, 0) < 2:
                seen[shape] = seen.get(shape, 0) + 1
                out.append(rule)
            if len(out) >= n:
                break
        return out

    def run_cycle(self, cycle: int, n_random: int = 40, n_mutants: int = 40) -> dict:
        before = self.session_trials
        self.phase = "probando reglas nuevas"
        for _ in range(n_random):
            self.evaluate_and_store(self.random_rule(), "random", cycle)
        self.phase = "mejorando las mejores"
        elites = self.elites()
        for i in range(n_mutants if elites else 0):
            self.evaluate_and_store(self.mutate(elites[i % len(elites)]), "evolution", cycle)
        validated = 0
        if self.can_validate:
            self.phase = "validando finalistas"
            for r in [x for x in self.top_rows(10, positive=True) if x.status == "EVALUATED"][:2]:
                self.check_stop()
                self.validate(r.id)
                validated += 1
        best = self.top_rows(1)
        out = {"cycle": cycle, "new_trials": self.session_trials - before, "validated": validated,
               "best_fitness": best[0].fitness if best else None}
        self.log(f"Intradía {self.dataset} · ciclo {cycle}: {out['new_trials']} reglas nuevas; mejor consistencia "
                 + (f"{out['best_fitness']:.3f}" if out["best_fitness"] is not None else "—"))
        return out

    def run(self, max_cycles: int = 0) -> int:
        with self.sf() as s:
            cycle = int(s.scalar(select(func.max(m.IntradayCandidate.cycle)).where(
                m.IntradayCandidate.universe_id == self.universe_id)) or 0) + 1
        self.log(f"{len(self.days)} acciones; búsqueda {self.search_sessions[0].date()} → {self.search_sessions[-1].date()} "
                 f"({len(self.search_sessions)} sesiones), pre-examen {len(self.pre_sessions)} sesiones, "
                 f"guardado para el test final desde {self.oos_start.date()} ({len(self.oos_sessions)} sesiones)")
        if not self.can_validate:
            self.log(f"Solo hay {len(self.search_sessions)} sesiones para buscar (mínimo {MIN_SEARCH_SESSIONS} para "
                     "validar): resultados orientativos. Actualiza las velas a menudo para acumular historia.")
        done = 0
        while not self.stop_event.is_set() and (max_cycles <= 0 or done < max_cycles):
            try:
                self.run_cycle(cycle)
            except StopRequested:
                break
            done, cycle = done + 1, cycle + 1
        if self.stop_event.is_set():
            self.log("Detenido por el usuario")
        self.phase = "parado"
        return done

    # ------------------------------------------------------------------ validation / pre-exam / final test
    def get_row(self, rid: str):
        with self.sf() as s:
            row = s.get(m.IntradayCandidate, rid)
        if row is None or row.universe_id != self.universe_id:
            raise KeyError(rid)
        return row

    def passive(self, sessions: pd.DatetimeIndex, symbols=None, oos: bool = False) -> dict:
        r = passive_returns(self._all_days if oos else self.days, sessions, symbols)
        return passive_summary(r, self.pc.bars_per_year)

    def validate(self, rid: str) -> dict:
        row = self.get_row(rid)
        if not self.can_validate:
            raise ValueError(f"hacen falta al menos {MIN_SEARCH_SESSIONS} sesiones de búsqueda para validar "
                             f"(hay {len(self.search_sessions)})")
        rule, mt = rule_from_dict(row.rule), row.metrics or {}
        d2, t2 = self.window(rule, self.search_sessions, cost_bps=2 * self.pc.cost_bps)
        costs2 = summarize(d2, t2, self.pc)
        kept = []
        for _ in range(6):  # neighbouring rules (one setting changed) should not collapse
            f, _m = self.score(self.mutate(rule))
            kept.append(f is not None and row.fitness is not None and row.fitness > 0 and f >= 0.5 * row.fitness)
        dp, tp = self.window(rule, self.pre_sessions)
        pre_m = summarize(dp, tp, self.pc, blocks=1)
        passive = self.passive(self.pre_sessions)
        pre = {**final_verdict({"total_return": pre_m["total_return"], "sharpe": pre_m["sharpe"]}, mt.get("sharpe"), passive),
               "metrics": {k: pre_m[k] for k in ("total_return", "sharpe", "max_drawdown", "n_trades")},
               "passive": passive, "period": [str(self.pre_sessions[0].date()), str(self.pre_sessions[-1].date())]}
        gates = {"min_trades": (mt.get("n_trades") or 0) >= 60, "costs_2x": costs2["sharpe"] > 0,
                 "robustness": float(np.mean(kept)) >= 0.5, "pre_exam": bool(pre["passed"])}
        passed = all(gates.values())
        val = _clean({"passed": passed, "gates": gates, "failed": sorted(k for k, v in gates.items() if not v),
                      "costs_2x": {"sharpe": costs2["sharpe"], "total_return": costs2["total_return"]},
                      "robustness": float(np.mean(kept)), "pre_exam": pre})
        with self.sf() as s, s.begin():
            r = s.get(m.IntradayCandidate, rid)
            r.validation, r.status = val, "VALIDATED_PASS" if passed else "VALIDATED_FAIL"
        self.log(f"Validación intradía {rid[:8]}: " + ("PASA" if passed else f"no pasa ({', '.join(val['failed'])})"))
        return val

    def final_test(self, rid: str) -> dict:
        row = self.get_row(rid)
        if row.status != "VALIDATED_PASS":
            raise ValueError("solo se puede hacer el test final a reglas que pasaron la validación")
        if len(self.oos_sessions) < MIN_OOS_SESSIONS:
            raise ValueError(f"el periodo guardado aún es corto ({len(self.oos_sessions)} sesiones, mínimo "
                             f"{MIN_OOS_SESSIONS}): sigue actualizando las velas")
        rule = rule_from_dict(row.rule)
        key = hash_obj({"orb": rule.version_id, "dataset": self.dataset, "epoch": self.epoch}, 32)
        with self.sf() as s, s.begin():
            if s.scalar(select(func.count()).select_from(m.OOSAccessLog).where(m.OOSAccessLog.strategy_version_id == key)):
                raise ValueError("esta regla ya tuvo su test final")
            s.add(m.OOSAccessLog(strategy_version_id=key, purpose=f"intraday {self.dataset} epoch {self.epoch} final test {rid}"))
        d, t = self.window(rule, self.oos_sessions, oos=True)
        oos = summarize(d, t, self.pc, blocks=1)
        passive = self.passive(self.oos_sessions, oos=True)
        research_sharpe = (row.metrics or {}).get("sharpe")
        v = final_verdict({"total_return": oos["total_return"], "sharpe": oos["sharpe"]}, research_sharpe, passive)
        decision = "FINAL_PASS" if v["passed"] else "FINAL_FAIL"
        final = _clean({"oos": {k: oos[k] for k in ("total_return", "sharpe", "max_drawdown", "n_trades", "win_rate")},
                        "passive": passive, "period": [str(self.oos_sessions[0].date()), str(self.oos_sessions[-1].date())],
                        "decision": decision, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "research_sharpe": research_sharpe, **v})
        with self.sf() as s, s.begin():
            r = s.get(m.IntradayCandidate, rid)
            r.final, r.status = final, decision
        self.log(f"Test final intradía {rid[:8]}: {'APROBADA' if v['passed'] else 'SUSPENDE'}")
        return final

    # ------------------------------------------------------------------ views
    def trial_stats(self) -> tuple[int, float]:
        """All trials (daily + intraday) count for Fiabilidad; the spread of results from the intraday ones."""
        with self.sf() as s:
            n_i = s.scalar(select(func.count()).select_from(m.IntradayCandidate)) or 0
            n_d = s.scalar(select(func.count()).select_from(m.ResearchCandidate)) or 0
            k, mean, mean2 = s.execute(select(func.count(m.IntradayCandidate.sr), func.avg(m.IntradayCandidate.sr),
                                              func.avg(m.IntradayCandidate.sr * m.IntradayCandidate.sr))).one()
        var = float((mean2 - mean ** 2) * k / (k - 1)) if k and k > 1 else 1.0 / max(len(self.search_sessions), 1)
        return int(n_i + n_d), max(var, 1e-12)

    def leaderboard(self, limit: int = 25, family: str | None = None) -> dict:
        n, var = self.trial_stats()
        passive = self.passive(self.search_sessions)
        sr0 = max(0.0, passive["sharpe"] / np.sqrt(self.pc.bars_per_year)) + expected_max_sharpe(max(n, 1), var)
        rows = []
        for r in self.top_rows(limit, family=family if family in FAMILIES else None):
            mt = r.metrics or {}
            rule = rule_from_dict(r.rule)
            dsr = psr_from_stats(mt["sr"], mt["skew"], mt["kurt"], mt["T"], sr0) if mt.get("T") else None
            rows.append({"id": r.id, "rules": rule.describe(), "rule": rule.to_dict(), "origin": r.origin,
                         "family": rule.family, "family_name": FAMILY_NAMES[rule.family],
                         "consistency": r.fitness, "sharpe": mt.get("sharpe"), "total_return": mt.get("total_return"),
                         "max_drawdown": mt.get("max_drawdown"), "n_trades": mt.get("n_trades"),
                         "win_rate": mt.get("win_rate"), "avg_trade": mt.get("avg_trade"),
                         "short_share": mt.get("short_share"), "days_traded": mt.get("days_traded"),
                         "blocks": mt.get("blocks"), "halves": mt.get("halves"), "complexity": mt.get("complexity"),
                         "dsr": dsr, "status": r.status, "validation": r.validation, "final": r.final})
        with self.sf() as s:
            per = dict(s.execute(select(m.IntradayCandidate.family, func.count()).where(
                m.IntradayCandidate.universe_id == self.universe_id).group_by(m.IntradayCandidate.family)).all())
        n_here = sum(per.values())
        by_family = {f: per.get(f, 0) + (per.get(None, 0) if f == "orb" else 0) for f in FAMILIES}
        return _clean({"dataset": self.dataset, "rows": rows, "n_trials": n, "n_trials_here": n_here,
                       "by_family": by_family, "family_names": FAMILY_NAMES,
                       "passive": passive, "symbols": len(self.days), "epoch": self.epoch,
                       "periods": {"search": [str(self.search_sessions[0].date()), str(self.search_sessions[-1].date())],
                                   "pre_exam": [str(self.pre_sessions[0].date()), str(self.pre_sessions[-1].date())],
                                   "oos_start": str(self.oos_start.date())},
                       "sessions": {"search": len(self.search_sessions), "pre_exam": len(self.pre_sessions),
                                    "oos": len(self.oos_sessions)},
                       "can_validate": self.can_validate, "can_final": len(self.oos_sessions) >= MIN_OOS_SESSIONS,
                       "min_sessions": {"search": MIN_SEARCH_SESSIONS, "oos": MIN_OOS_SESSIONS},
                       "costs": {"bps_per_side": self.pc.cost_bps, "max_positions": self.pc.max_positions,
                                 "risk_per_trade": self.pc.risk_per_trade}})

    def curve(self, rid: str) -> dict:
        """Equity curve of the research period (plus the stored period once the rule had its final test)."""
        row = self.get_row(rid)
        rule = rule_from_dict(row.rule)
        with_oos = row.status in ("FINAL_PASS", "FINAL_FAIL")
        sess = self.sessions if with_oos else self.research_sessions
        d, tr = self.window(rule, sess, oos=with_oos)
        eq = (1 + d).cumprod() * 10_000
        return _clean({"id": rid, "rules": rule.describe(), "includes_oos": with_oos,
                       "equity": [{"time": int(t.timestamp()), "value": float(v)} for t, v in eq.items()],
                       "pre_exam_start": str(self.pre_sessions[0].date()), "oos_start": str(self.oos_start.date()),
                       "trades": [{"date": str(t.session.date()), "symbol": t.symbol,
                                   "side": "largo" if t.direction > 0 else "corto", "entry_min": int(t.entry_min),
                                   "exit": EXIT_KIND[int(t.exit)], "r": float(t.r), "net": float(t.net),
                                   "weight": float(t.weight)}
                                  for t in tr.tail(300).iloc[::-1].itertuples(index=False)]})


# ---------------------------------------------------------------------- background runner
@dataclass
class IntradayRunnerState:
    running: bool = False
    dataset: str | None = None
    phase: str = "parado"
    error: str | None = None
    started_at: str | None = None


class IntradayRunner:
    def __init__(self, build: Callable[[str, Callable[[str], None], threading.Event], IntradayLab],
                 on_finish: Callable[[], None] | None = None):
        self._build, self._lock, self.on_finish = build, threading.Lock(), on_finish
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = IntradayRunnerState()
        self.logs: deque[str] = deque(maxlen=200)
        self.lab: IntradayLab | None = None

    def log(self, msg: str) -> None:
        self.logs.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    def start(self, dataset: str, max_cycles: int = 0) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop = threading.Event()
            self.lab = None
            self.state = IntradayRunnerState(running=True, dataset=dataset, phase="cargando velas",
                                             started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self._thread = threading.Thread(target=self._run, args=(dataset, max_cycles), daemon=True, name="intraday")
            self._thread.start()
            return True

    def _run(self, dataset: str, max_cycles: int) -> None:
        from qsts.core.power import keep_awake
        keep_awake(True)
        try:
            self.log(f"Cargando velas de {dataset}…")
            self.lab = self._build(dataset, self.log, self._stop)
            self.lab.run(max_cycles)
        except StopRequested:
            self.log("Detenido por el usuario")
        except Exception as e:  # noqa: BLE001
            self.state.error = (str(e) or repr(e))[:400]
            self.log(f"ERROR: {e}"[:300])
        finally:
            keep_awake(False)
            self.state.running, self.state.phase = False, "parado"
            if self.on_finish:
                self.on_finish()

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def status(self) -> dict:
        st = asdict(self.state)
        lab = self.lab
        if lab is not None and self.state.running:
            st["phase"] = lab.phase
            st["session_trials"] = lab.session_trials
        st["log"] = list(self.logs)[-60:]
        return st
