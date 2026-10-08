"""Portfolio backtest: one strategy run as a scanner over a universe of stocks (the S&P 500).

Every session, after the close, the strategy's signals are computed on every stock of the universe; at the next
open the open positions are managed exactly as in the single-stock backtest (qsts.lab.backtest: resting stops and
targets, gaps fill at the open, stop first when a bar touches both, exits and reversals at the open) and the free
places (at most `max_positions`) are filled with the stocks that signalled an entry, best `rank` first (the
author's rule or the fixed one, see qsts.lab.strategy). Each new position gets 1/`max_positions` of the portfolio's
equity at the previous close, never more than the equity not already invested (no leverage).

Survivorship: the universe is today's S&P 500 list (companies that left the index are missing, which flatters
results). To reduce it, a stock is only traded from the day it joined the index ("date added"), never before.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qsts.lab.backtest import _ts, entry_fill
from qsts.lab.strategy import FLOAT_COLUMNS, MAX_POSITIONS

FLOAT_SIGNALS = FLOAT_COLUMNS


@dataclass
class PortfolioConfig:
    initial_capital: float = 10_000.0
    max_positions: int = MAX_POSITIONS
    commission_pct: float = 0.0
    slippage_bps: float = 5.0
    start: str | None = None
    end: str | None = None

    def __post_init__(self):
        if not 1 <= int(self.max_positions) <= MAX_POSITIONS:
            raise ValueError(f"las posiciones a la vez deben estar entre 1 y {MAX_POSITIONS}")
        self.max_positions = int(self.max_positions)


@dataclass
class Panel:
    """Every stock's bars and signals on one calendar (T sessions x N stocks; NaN where a stock has no bar)."""
    index: pd.DatetimeIndex
    symbols: list[str]
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    entry: np.ndarray        # int8
    exit_long: np.ndarray    # bool
    exit_short: np.ndarray   # bool
    rank: np.ndarray
    eligible: np.ndarray     # bool: in the index that day (from its date added) and with a bar
    floats: dict = field(default_factory=dict)  # stop / target / stop_pct / target_pct / trail (only if used)
    last_bar: np.ndarray | None = None          # last row with a bar, per stock
    day_trade: bool = False                     # positions closed at the close of their entry day

    def f(self, name: str, i: int, k: int) -> float:
        a = self.floats.get(name)
        return float(a[i, k]) if a is not None else np.nan

    def column(self, symbol: str) -> int:
        return self.symbols.index(symbol)


class PanelBuilder:
    """Fills a Panel one stock at a time (so the whole universe's DataFrames never sit in memory together)."""

    def __init__(self, calendar: pd.DatetimeIndex, symbols: list[str], day_trade: bool = False):
        self.index, self.symbols = calendar, list(symbols)
        T, N = len(calendar), len(self.symbols)
        nan = lambda: np.full((T, N), np.nan)  # noqa: E731
        self.p = Panel(calendar, self.symbols, nan(), nan(), nan(), nan(), np.zeros((T, N), np.int8),
                       np.zeros((T, N), bool), np.zeros((T, N), bool), nan(), np.zeros((T, N), bool))
        self.p.last_bar = np.full(N, -1)
        self.p.day_trade = day_trade

    def add(self, symbol: str, bars: pd.DataFrame, sig: pd.DataFrame, start: pd.Timestamp | None) -> None:
        k = self.symbols.index(symbol)
        rows = self.index.get_indexer(bars.index)
        ok = rows >= 0
        r = rows[ok]
        p = self.p
        for name, arr in (("open", p.o), ("high", p.h), ("low", p.l), ("close", p.c)):
            arr[r, k] = bars[name].to_numpy(dtype=float)[ok]
        p.entry[r, k] = sig["entry"].to_numpy()[ok]
        p.exit_long[r, k] = sig["exit_long"].to_numpy()[ok]
        p.exit_short[r, k] = sig["exit_short"].to_numpy()[ok]
        p.rank[r, k] = sig["rank"].to_numpy(dtype=float)[ok]
        member = np.ones(len(bars), bool) if start is None else (bars.index >= start)
        p.eligible[r, k] = member[ok] & np.isfinite(bars["open"].to_numpy(dtype=float)[ok])
        for name in FLOAT_SIGNALS:
            v = sig[name].to_numpy(dtype=float)[ok]
            if np.isfinite(v).any():
                if name not in p.floats:
                    p.floats[name] = np.full(p.o.shape, np.nan)
                p.floats[name][r, k] = v
        if len(r):
            p.last_bar[k] = int(r.max())

    def done(self) -> Panel:
        return self.p


@dataclass
class PortfolioResult:
    equity: pd.Series
    trades: pd.DataFrame      # one row per closed trade, with the stock in `symbol`
    position: pd.Series       # number of open positions at each close
    config: PortfolioConfig
    open_trades: list[dict]   # positions still open at the last bar (marked at its close)


TRADE_COLUMNS = ["symbol", "side", "entry_time", "entry_index", "entry_price", "qty", "entry_fee", "entry_equity",
                 "signal_time", "exit_time", "exit_price", "exit_index", "pnl", "pnl_pct", "return", "bars", "reason"]


def run_portfolio(panel: Panel, cfg: PortfolioConfig | None = None, symbols_mask: np.ndarray | None = None,
                  monkey: dict | None = None) -> PortfolioResult:
    """`symbols_mask`: only these stocks may be bought (the split check). `monkey`: random entries instead of the
    strategy's ({"p": daily entry probability, "hold": holding periods to draw from, "short": share of shorts,
    "seed": int}), same places and sizing, no stops: the 'monkey' benchmark."""
    cfg = cfg or PortfolioConfig()
    o, h, l, c = panel.o, panel.h, panel.l, panel.c  # noqa: E741
    T, N = o.shape
    idx = panel.index
    lo = _ts(cfg.start) if cfg.start else idx[0]
    hi = _ts(cfg.end) if cfg.end else idx[-1]
    rows = np.flatnonzero((idx >= lo) & (idx <= hi))
    slip, comm, maxp = cfg.slippage_bps / 1e4, cfg.commission_pct / 100, cfg.max_positions
    rng = np.random.default_rng(monkey.get("seed", 0)) if monkey else None
    allowed = np.ones(N, bool) if symbols_mask is None else symbols_mask
    last_bar = panel.last_bar if panel.last_bar is not None else np.full(N, T - 1)

    cash = cfg.initial_capital
    pos: dict[int, dict] = {}
    trades: list[dict] = []
    last_close = np.full(N, np.nan)
    eq = np.full(T, np.nan)
    npos = np.zeros(T)

    def close(k: int, i: int, price: float, reason: str, market: bool) -> None:
        nonlocal cash
        p = pos.pop(k)
        q = p["qty"]
        side = np.sign(q)
        fill = price * (1 - side * slip) if market else price
        fee = abs(q) * fill * comm
        cash += q * fill - fee
        t = p["trade"]
        pnl = q * (fill - t["entry_price"]) - fee - t["entry_fee"]
        trades.append({**t, "exit_time": idx[i], "exit_price": fill, "exit_index": i, "pnl": pnl,
                       "pnl_pct": pnl / (abs(q) * t["entry_price"]), "return": pnl / t["entry_equity"],
                       "bars": i - t["entry_index"], "reason": reason})

    def open_(k: int, i: int, j: int, direction: int, amount: float, equity_now: float, fill: float) -> None:
        nonlocal cash
        units = amount / fill
        fee = units * fill * comm
        q = direction * units
        cash -= q * fill + fee
        stop = target = np.nan
        if not monkey:
            sa, sp = panel.f("stop", j, k), panel.f("stop_pct", j, k)
            ta, tp = panel.f("target", j, k), panel.f("target_pct", j, k)
            stop = sa if np.isfinite(sa) else (fill * (1 - direction * sp) if np.isfinite(sp) else np.nan)
            target = ta if np.isfinite(ta) else (fill * (1 + direction * tp) if np.isfinite(tp) else np.nan)
            if np.isfinite(stop) and direction * (fill - stop) <= 0:
                stop = np.nan
            if np.isfinite(target) and direction * (target - fill) <= 0:
                target = np.nan
        hold = int(max(1, rng.choice(monkey["hold"]))) if monkey else None
        pos[k] = {"qty": q, "stop": stop, "target": target, "hold": hold,
                  "trade": {"symbol": panel.symbols[k], "side": "largo" if direction > 0 else "corto",
                            "entry_time": idx[i], "entry_index": i, "entry_price": fill, "qty": units,
                            "entry_fee": fee, "entry_equity": equity_now, "signal_time": idx[j]}}

    first = True
    for i in rows:
        if not first:
            j = i - 1
            for k in list(pos):  # 1) open positions at today's open
                p = pos[k]
                if i > last_bar[k]:  # the stock has no more bars (data ends): closed at its last close
                    close(k, i, last_close[k], "data_end", market=False)
                    continue
                if not np.isfinite(o[i, k]):
                    continue
                d = np.sign(p["qty"])
                if np.isfinite(p["stop"]) and d * (o[i, k] - p["stop"]) <= 0:
                    close(k, i, o[i, k], "stop_gap", market=True)
                elif np.isfinite(p["target"]) and d * (o[i, k] - p["target"]) >= 0:
                    close(k, i, o[i, k], "target_gap", market=False)
                elif monkey:
                    if i - p["trade"]["entry_index"] >= p["hold"]:
                        close(k, i, o[i, k], "signal", market=True)
                elif panel.exit_long[j, k] if d > 0 else panel.exit_short[j, k]:
                    close(k, i, o[i, k], "signal", market=True)
                elif panel.entry[j, k] == -d:
                    close(k, i, o[i, k], "reverse", market=True)
            free = maxp - len(pos)
            if free > 0:  # 2) free places: the best-ranked new signals of the previous close
                ok = panel.eligible[j] & np.isfinite(o[i]) & allowed
                if monkey:
                    hit = ok & (rng.random(N) < monkey["p"])
                    ks = rng.permutation(np.flatnonzero(hit))
                    dirs = np.where(rng.random(len(ks)) < monkey.get("short", 0.0), -1, 1)
                else:
                    ks = np.flatnonzero(ok & (panel.entry[j] != 0))
                    r = panel.rank[j, ks]
                    ks = ks[np.lexsort((ks, np.where(np.isfinite(r), -r, np.inf)))]
                    dirs = panel.entry[j, ks].astype(int)
                if len(ks):
                    held = list(pos)
                    marks = np.array([last_close[k] for k in held])
                    qtys = np.array([pos[k]["qty"] for k in held])
                    equity_prev = cash + float((qtys * marks).sum()) if held else cash
                    gross = float((np.abs(qtys) * marks).sum()) if held else 0.0
                    for k, d in zip(ks, dirs):
                        if free <= 0:
                            break
                        if k in pos:
                            continue
                        amount = min(equity_prev / maxp, equity_prev - gross)
                        if amount <= equity_prev * 1e-4:
                            break
                        free -= 1  # an order is sent for this place; a stop / limit order may not be executed
                        fill = (o[i, k] * (1 + d * slip) if monkey else
                                entry_fill(int(d), o[i, k], h[i, k], l[i, k], panel.f("entry_stop", j, k),
                                           panel.f("entry_limit", j, k), slip))
                        if fill is None:
                            continue
                        open_(int(k), i, j, int(d), amount, equity_prev, fill)
                        gross += amount
            for k in list(pos):  # 3) resting orders inside the bar (stop first when both are touched)
                if not np.isfinite(o[i, k]):
                    continue
                p = pos[k]
                d = np.sign(p["qty"])
                hit_stop = np.isfinite(p["stop"]) and ((l[i, k] <= p["stop"]) if d > 0 else (h[i, k] >= p["stop"]))
                hit_tgt = np.isfinite(p["target"]) and ((h[i, k] >= p["target"]) if d > 0 else (l[i, k] <= p["target"]))
                if hit_stop:
                    close(k, i, p["stop"], "stop", market=True)
                elif hit_tgt:
                    close(k, i, p["target"], "target", market=False)
            if panel.day_trade:  # 4) intraday strategy: everything out at the close (market-on-close)
                for k in list(pos):
                    if np.isfinite(c[i, k]):
                        close(k, i, c[i, k], "close", market=True)
        first = False
        tr = panel.floats.get("trail")
        if tr is not None and not monkey:
            for k, p in pos.items():
                t = tr[i, k]
                if np.isfinite(t):
                    d = np.sign(p["qty"])
                    s = p["stop"]
                    p["stop"] = t if not np.isfinite(s) else (max(s, t) if d > 0 else min(s, t))
        today = np.isfinite(c[i])
        last_close[today] = c[i, today]
        eq[i] = cash + sum(p["qty"] * last_close[k] for k, p in pos.items())
        npos[i] = len(pos)

    open_trades = []
    if len(rows):
        last = int(rows[-1])
        for k, p in pos.items():
            t, q = p["trade"], p["qty"]
            unreal = q * (last_close[k] - t["entry_price"]) - t["entry_fee"]
            open_trades.append({**t, "exit_time": idx[last], "exit_price": last_close[k], "exit_index": last,
                                "pnl": unreal, "pnl_pct": unreal / (abs(q) * t["entry_price"]),
                                "return": unreal / t["entry_equity"], "bars": last - t["entry_index"], "reason": "end",
                                "stop": p["stop"], "target": p["target"], "direction": int(np.sign(q))})
    keep = np.zeros(T, bool)
    keep[rows] = True
    keep &= ~np.isnan(eq)
    return PortfolioResult(equity=pd.Series(eq[keep], index=idx[keep]),
                           trades=pd.DataFrame(trades, columns=TRADE_COLUMNS),
                           position=pd.Series(npos[keep], index=idx[keep]), config=cfg,
                           open_trades=sorted(open_trades, key=lambda x: x["symbol"]))


def candidates(panel: Panel, day: int, exclude: set[str] = frozenset()) -> list[dict]:
    """The stocks whose signal at the close of row `day` asks for an entry, best ranked first (what the scanner
    would buy at the next open if there were free places)."""
    ok = panel.eligible[day] & (panel.entry[day] != 0)
    ks = np.flatnonzero(ok)
    r = panel.rank[day, ks]
    ks = ks[np.lexsort((ks, np.where(np.isfinite(r), -r, np.inf)))]
    out = []
    for k in ks:
        sym = panel.symbols[k]
        if sym in exclude:
            continue
        out.append({"symbol": sym, "direction": int(panel.entry[day, k]), "rank": float(panel.rank[day, k]),
                    "close": float(panel.c[day, k]), **{n: panel.f(n, day, k) for n in FLOAT_SIGNALS}})
    return out


def snapshot(panel: Panel, days: int = 10) -> dict:
    """The last `days` sessions of every stock's signals (what the paper trader needs; panels are too big to keep)."""
    T = len(panel.index)
    lo = max(0, T - days)
    out = {}
    for k, sym in enumerate(panel.symbols):
        rows = {}
        for i in range(lo, T):
            if not np.isfinite(panel.c[i, k]):
                continue
            rows[str(panel.index[i].date())] = {
                "close": float(panel.c[i, k]), "entry": int(panel.entry[i, k]),
                "exit_long": bool(panel.exit_long[i, k]), "exit_short": bool(panel.exit_short[i, k]),
                "rank": float(panel.rank[i, k]), "eligible": bool(panel.eligible[i, k]),
                **{n: panel.f(n, i, k) for n in FLOAT_SIGNALS}}
        if rows:
            out[sym] = rows
    return out


def monkey_spec(panel: Panel, trades: pd.DataFrame, rows: np.ndarray | None = None) -> dict | None:
    """Same entry frequency, holding periods and long/short mix as the strategy, but random stocks and days."""
    if len(trades) < 5:
        return None
    el = panel.eligible if rows is None else panel.eligible[rows]
    en = panel.entry if rows is None else panel.entry[rows]
    n_el = int(el.sum())
    if n_el == 0:
        return None
    p = float(((en != 0) & el).sum()) / n_el
    return {"p": p, "hold": np.clip(trades["bars"].to_numpy(dtype=int), 1, None),
            "short": float((trades["side"] == "corto").mean())}


def breadth_summary(rows: list[dict]) -> dict:
    """The strategy on each stock on its own (100% of a separate account per stock, from its date added): does it
    work on most of them or only on a few?"""
    used = [r for r in rows if r["trades"] >= 3]
    if not used:
        return {"n_symbols": len(rows), "n_used": 0}
    pf = [r for r in used if (r["profit_factor"] or 0) > 1 or (r["profit_factor"] is None and r["net"] > 0)]
    beats = [r for r in used if r["hold"] is not None and r["net"] > r["hold"]]
    wins = sum(r["wins"] for r in used)
    gp = sum(r["gross_profit"] for r in used)
    gl = sum(r["gross_loss"] for r in used)
    n_tr = sum(r["trades"] for r in used)
    return {"n_symbols": len(rows), "n_used": len(used), "profitable": len(pf) / len(used),
            "beats_hold": len(beats) / len(used), "median_net": float(np.median([r["net"] for r in used])),
            "median_pf": float(np.median([r["profit_factor"] for r in used if r["profit_factor"] is not None]))
            if any(r["profit_factor"] is not None for r in used) else None,
            "trades": n_tr, "win_rate": wins / n_tr if n_tr else None, "pooled_pf": gp / gl if gl > 0 else None,
            "avg_trade_pct": float(np.average([r["avg_trade_pct"] for r in used if r["avg_trade_pct"] is not None],
                                              weights=[r["trades"] for r in used if r["avg_trade_pct"] is not None]))
            if any(r["avg_trade_pct"] is not None for r in used) else None}
