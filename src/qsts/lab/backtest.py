"""Backtest of one strategy on one stock, with TradingView's default execution model.

- A signal is decided at a bar's close and filled at the NEXT bar's open (market order), like `strategy()` with
  `process_orders_on_close=false`. One position at a time (no pyramiding); an opposite entry reverses it.
- Stops and targets are resting orders: checked at the next open first (a gap through the level fills at the open,
  which is worse for a stop and better for a target, as a real order would), then inside each bar with its
  high/low. A bar that touches both counts as the STOP (the order inside a daily bar is unknown, so the result is
  never flattered). They are also checked on the entry bar itself.
- No leverage: a position uses `size_pct` (at most 100%) of the bot's current equity. Costs per side: commission
  (% of the traded value; Alpaca charges none on US stocks) and slippage on market and stop fills.
- Prices are the stored daily bars adjusted for splits and dividends (qsts.data.adjust), so holding through a
  dividend is rewarded (or paid, when short) as in reality.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from qsts.lab.strategy import Strategy

REASONS = {"signal": "señal", "reverse": "señal contraria", "stop": "stop", "stop_gap": "stop (hueco de apertura)",
           "target": "objetivo", "target_gap": "objetivo (hueco de apertura)", "end": "abierta (fin de datos)",
           "data_end": "sin más datos de la acción", "close": "cierre del día (intradía)"}


def entry_fill(direction: int, o: float, h: float, l: float, stop_level: float, limit_level: float,  # noqa: E741
               slip: float) -> float | None:
    """Fill price of an entry at this bar, or None when its order is not executed.
    Market at the open (slippage against us); a stop order fills at the level, or at the open when the open has
    already gone through it (slippage applies: it becomes a market order); a limit-on-open fills at the open only
    when the open is at or better than the limit (the opening auction price, no slippage)."""
    if np.isfinite(stop_level):
        if direction > 0:
            px = o if o >= stop_level else (stop_level if h >= stop_level else None)
        else:
            px = o if o <= stop_level else (stop_level if l <= stop_level else None)
        return None if px is None else px * (1 + direction * slip)
    if np.isfinite(limit_level):
        ok = o <= limit_level if direction > 0 else o >= limit_level
        return o if ok else None
    return o * (1 + direction * slip)


@dataclass
class BacktestConfig:
    initial_capital: float = 10_000.0
    size_pct: float = 100.0       # % of the bot's equity per position (no leverage: at most 100)
    commission_pct: float = 0.0   # % of traded value per side
    slippage_bps: float = 5.0     # per side, on market and stop fills
    start: str | None = None
    end: str | None = None

    def __post_init__(self):
        if not 0 < self.size_pct <= 100:
            raise ValueError("size_pct must be in (0, 100]: no leverage")


@dataclass
class BacktestResult:
    equity: pd.Series            # bot equity at each close
    trades: pd.DataFrame         # one row per closed trade (+ the open one, reason "end")
    position: pd.Series          # signed position units at each close
    signals: pd.DataFrame
    config: BacktestConfig
    open_trade: dict | None      # the position still open at the last bar, if any

    def to_dict(self) -> dict:
        return {"config": asdict(self.config), "n_trades": int(len(self.trades))}


def run_backtest(strategy: Strategy, bars: pd.DataFrame, params: dict | None = None,
                 cfg: BacktestConfig | None = None) -> BacktestResult:
    """`bars`: daily OHLC (UTC session index). Signals are computed on the WHOLE history (indicators warm up),
    trading happens only inside [start, end]."""
    cfg = cfg or BacktestConfig()
    sig = strategy.run(bars, params)
    lo = _ts(cfg.start) if cfg.start else bars.index[0]
    hi = _ts(cfg.end) if cfg.end else bars.index[-1]
    window = (bars.index >= lo) & (bars.index <= hi)
    return _simulate(bars, sig, window, cfg, day_trade=strategy.day_trade)


def _simulate(bars: pd.DataFrame, sig: pd.DataFrame, window: np.ndarray, cfg: BacktestConfig,
              day_trade: bool = False) -> BacktestResult:
    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))  # noqa: E741
    entry = sig["entry"].to_numpy()
    exit_long, exit_short = sig["exit_long"].to_numpy(), sig["exit_short"].to_numpy()
    stop_abs, tgt_abs = sig["stop"].to_numpy(), sig["target"].to_numpy()
    stop_pct, tgt_pct = sig["stop_pct"].to_numpy(), sig["target_pct"].to_numpy()
    trail = sig["trail"].to_numpy()
    e_stop = sig["entry_stop"].to_numpy() if "entry_stop" in sig else np.full(len(sig), np.nan)
    e_limit = sig["entry_limit"].to_numpy() if "entry_limit" in sig else np.full(len(sig), np.nan)
    slip, comm = cfg.slippage_bps / 1e4, cfg.commission_pct / 100
    idx = bars.index
    n = len(bars)

    cash, qty = cfg.initial_capital, 0.0
    stop = target = np.nan
    trade: dict | None = None
    trades: list[dict] = []
    eq = np.full(n, np.nan)
    pos = np.zeros(n)
    started = False

    def close_position(i: int, price: float, reason: str, market: bool) -> None:
        nonlocal cash, qty, stop, target, trade
        side = np.sign(qty)
        fill = price * (1 - side * slip) if market else price  # selling a long fills lower, covering a short higher
        fee = abs(qty) * fill * comm
        cash += qty * fill - fee
        pnl = qty * (fill - trade["entry_price"]) - fee - trade["entry_fee"]
        trades.append({**trade, "exit_time": idx[i], "exit_price": fill, "exit_index": i, "pnl": pnl,
                       "pnl_pct": pnl / (abs(qty) * trade["entry_price"]), "return": pnl / trade["entry_equity"],
                       "bars": i - trade["entry_index"], "reason": reason})
        qty, stop, target, trade = 0.0, np.nan, np.nan, None

    def open_position(i: int, direction: int, j: int, fill: float) -> None:
        """Enter at bar i (at `fill`) on the signal of bar j (= i - 1)."""
        nonlocal cash, qty, stop, target, trade
        equity_now = cash
        units = equity_now * cfg.size_pct / 100 / fill
        fee = units * fill * comm
        qty = direction * units
        cash -= qty * fill + fee
        stop = stop_abs[j] if np.isfinite(stop_abs[j]) else (
            fill * (1 - direction * stop_pct[j]) if np.isfinite(stop_pct[j]) else np.nan)
        target = tgt_abs[j] if np.isfinite(tgt_abs[j]) else (
            fill * (1 + direction * tgt_pct[j]) if np.isfinite(tgt_pct[j]) else np.nan)
        if np.isfinite(stop) and direction * (fill - stop) <= 0:
            stop = np.nan  # a stop on the wrong side of the fill would exit at once: ignored
        if np.isfinite(target) and direction * (target - fill) <= 0:
            target = np.nan
        trade = {"side": "largo" if direction > 0 else "corto", "entry_time": idx[i], "entry_index": i,
                 "entry_price": fill, "qty": units, "entry_fee": fee, "entry_equity": equity_now,
                 "signal_time": idx[j]}

    for i in range(n):
        if not window[i]:
            if started:  # past the end of the window: close at the last bar inside it (handled below)
                break
            continue
        if started:
            j = i - 1
            if qty != 0:  # 1) resting orders at the open: a gap through the level fills at the open
                d = np.sign(qty)
                if np.isfinite(stop) and d * (o[i] - stop) <= 0:
                    close_position(i, o[i], "stop_gap", market=True)
                elif np.isfinite(target) and d * (o[i] - target) >= 0:
                    close_position(i, o[i], "target_gap", market=False)
            if qty != 0:  # 2) exit / reverse signal of the previous close
                d = np.sign(qty)
                if exit_long[j] if d > 0 else exit_short[j]:
                    close_position(i, o[i], "signal", market=True)
                elif entry[j] == -d:
                    close_position(i, o[i], "reverse", market=True)
            if qty == 0 and entry[j] != 0:  # 3) new entry (market at the open, stop or limit-on-open order)
                fill = entry_fill(int(entry[j]), o[i], h[i], l[i], e_stop[j], e_limit[j], slip)
                if fill is not None:
                    open_position(i, int(entry[j]), j, fill)
            if qty != 0:  # 4) resting orders inside the bar (stop first when both are touched)
                d = np.sign(qty)
                hit_stop = np.isfinite(stop) and ((l[i] <= stop) if d > 0 else (h[i] >= stop))
                hit_tgt = np.isfinite(target) and ((h[i] >= target) if d > 0 else (l[i] <= target))
                if hit_stop:
                    close_position(i, stop, "stop", market=True)
                elif hit_tgt:
                    close_position(i, target, "target", market=False)
            if day_trade and qty != 0:  # 5) intraday strategy: out at the close of the day (market-on-close)
                close_position(i, c[i], "close", market=True)
        started = True
        if qty != 0 and np.isfinite(trail[i]):  # trailing stop for the next bars: only in the position's favour
            d = np.sign(qty)
            stop = trail[i] if not np.isfinite(stop) else (max(stop, trail[i]) if d > 0 else min(stop, trail[i]))
        eq[i] = cash + qty * c[i]
        pos[i] = qty

    last = int(np.flatnonzero(window)[-1]) if window.any() else -1
    open_trade = None
    if trade is not None and last >= 0:  # report the open position marked at the last close
        d = np.sign(qty)
        unreal = qty * (c[last] - trade["entry_price"]) - trade["entry_fee"]
        open_trade = {**trade, "exit_time": idx[last], "exit_price": c[last], "exit_index": last, "pnl": unreal,
                      "pnl_pct": unreal / (abs(qty) * trade["entry_price"]), "return": unreal / trade["entry_equity"],
                      "bars": last - trade["entry_index"], "reason": "end", "stop": stop, "target": target,
                      "direction": int(d)}
    keep = window & ~np.isnan(eq)
    tr = pd.DataFrame(trades, columns=["side", "entry_time", "entry_index", "entry_price", "qty", "entry_fee",
                                       "entry_equity", "signal_time", "exit_time", "exit_price", "exit_index",
                                       "pnl", "pnl_pct", "return", "bars", "reason"])
    return BacktestResult(equity=pd.Series(eq[keep], index=idx[keep]), trades=tr,
                          position=pd.Series(pos[keep], index=idx[keep]), signals=sig[keep], config=cfg,
                          open_trade=open_trade)


def buy_and_hold(bars: pd.DataFrame, start=None, end=None, capital: float = 10_000.0) -> pd.Series:
    """Equity of buying at the first close of the window and holding (the bar every strategy has to beat)."""
    c = bars["close"]
    if start is not None:
        c = c[c.index >= _ts(start)]
    if end is not None:
        c = c[c.index <= _ts(end)]
    return capital * c / c.iloc[0] if len(c) else c


def _ts(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")
