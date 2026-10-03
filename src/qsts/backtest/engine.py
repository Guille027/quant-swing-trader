"""Event-driven, portfolio-level backtest engine.

Timing model (the core look-ahead protection):
  * Strategy decisions are taken at the CLOSE of bar t, from data <= t.
  * Market orders created at bar t are filled at the OPEN of the symbol's next bar (t+1).
    This models latency: no fill can happen at a price the strategy used to decide.
  * Stops / targets are checked intrabar from the bar after entry onward (and on the entry bar
    itself, after the open). If the bar opens beyond the stop (overnight gap) the fill is the open,
    not the stop. If stop and target are both touched in one bar the STOP is assumed first
    (conservative: OHLC data cannot tell the order).
  * Trailing stops are updated at bar close and become effective on the next bar.

Costs: commission (per share / percent / minimum), half-spread per side, slippage, short borrow
fees, and a volume-participation cap that produces partial fills.

Prices are taken as given; pass split/dividend adjusted bars (see qsts.data.adjust) so splits do
not create fake gaps. Share-count limits (no fractional) are applied to those prices.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from qsts.strategy.definition import CompiledStrategy, StrategyDefinition


@dataclass(frozen=True)
class CostModel:
    commission_per_share: float = 0.0
    commission_pct: float = 0.0
    commission_min: float = 0.0
    # Full quoted spread in basis points; half is paid on each side.
    spread_bps: float = 5.0
    # Additional adverse slippage in basis points per fill.
    slippage_bps: float = 5.0
    # Annual borrow fee for shorts (fraction of position value).
    borrow_rate_annual: float = 0.0
    # Max fraction of a bar's volume one order may take. Larger orders fill partially.
    max_volume_participation: float = 0.01

    def commission(self, qty: float, price: float) -> float:
        if qty <= 0:
            return 0.0
        c = qty * self.commission_per_share + qty * price * self.commission_pct
        return max(c, self.commission_min)

    def fill_price(self, ref: float, side: int) -> float:
        """side=+1 buying, -1 selling: always adverse."""
        return ref * (1 + side * (self.spread_bps / 2 + self.slippage_bps) / 1e4)


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 10_000.0
    risk_per_trade: float = 0.01  # fraction of equity lost if the stop is hit (before costs/gaps)
    max_position_pct: float = 0.20  # max notional per position as fraction of equity
    max_positions: int = 10
    max_gross_exposure: float = 1.0  # gross notional / equity (1.0 = no leverage)
    allow_fractional: bool = True
    min_qty: float = 1e-6
    bars_per_year: int = 252
    costs: CostModel = field(default_factory=CostModel)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Pos:
    symbol: str
    direction: int
    qty: float
    entry_ts: pd.Timestamp
    entry_price: float
    stop: float
    target: float | None
    stop_dist: float
    initial_risk: float
    costs: float
    bars: int = 0
    mae: float = 0.0
    mfe: float = 0.0
    pending_exit: str | None = None
    pending_exit_qty: float = 0.0


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.DataFrame  # index ts: equity, cash, gross_exposure, n_positions
    config: dict
    strategy_version: str
    rejected_orders: list[dict]

    @property
    def returns(self) -> pd.Series:
        return self.equity["equity"].pct_change().fillna(0.0)


class BacktestEngine:
    def __init__(self, cfg: BacktestConfig = BacktestConfig()):
        self.cfg = cfg

    def run(self, strategy: StrategyDefinition, data: dict[str, pd.DataFrame],
            regime: pd.Series | None = None, start=None, end=None) -> BacktestResult:
        cs = CompiledStrategy(strategy)
        cfg, cm = self.cfg, self.cfg.costs
        frames, sigs = {}, {}
        for sym, df in sorted(data.items()):
            sig = cs.evaluate(df, regime)  # computed on full history -> warm-up before `start`
            if start is not None:
                keep = df.index >= pd.Timestamp(start)
                df, sig = df[keep], sig[keep]
            if end is not None:
                keep = df.index <= pd.Timestamp(end)
                df, sig = df[keep], sig[keep]
            if len(df):
                frames[sym], sigs[sym] = df, sig
        timeline = sorted(set().union(*[f.index for f in frames.values()])) if frames else []
        # numpy views: row access via .iloc is ~100x slower and dominated run time
        B = {sym: {k: df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume")}
             for sym, df in frames.items()}
        SIG = {sym: {k: sg[k].to_numpy() for k in sg.columns} for sym, sg in sigs.items()}
        IDX = {sym: list(df.index) for sym, df in frames.items()}

        cash = cfg.initial_capital
        pos: dict[str, _Pos] = {}
        pending_entries: dict[str, dict] = {}
        last_close: dict[str, float] = {}
        trades, eq_rows, rejected = [], [], []
        ptr = {s: 0 for s in frames}
        borrow_per_bar = cm.borrow_rate_annual / cfg.bars_per_year

        def equity_now() -> float:
            return cash + sum(p.direction * p.qty * last_close[p.symbol] for p in pos.values())

        def close_qty(p: _Pos, qty: float, ts, raw_price: float, reason: str, apply_cost: bool = True):
            nonlocal cash
            side = -p.direction  # selling a long / buying back a short
            px = cm.fill_price(raw_price, side) if apply_cost else raw_price
            comm = cm.commission(qty, px)
            cash += p.direction * qty * px - comm  # long: +proceeds ; short: -cost to cover
            frac = qty / p.qty
            entry_cost_share = p.costs * frac
            gross = p.direction * qty * (px - p.entry_price)
            pnl = gross - comm - entry_cost_share
            trades.append({
                "symbol": p.symbol, "direction": "LONG" if p.direction > 0 else "SHORT",
                "entry_ts": p.entry_ts, "exit_ts": ts, "entry_price": p.entry_price, "exit_price": px,
                "qty": qty, "pnl": pnl, "costs": comm + entry_cost_share,
                "r_multiple": pnl / (p.initial_risk * frac) if p.initial_risk > 0 else np.nan,
                "return_pct": p.direction * (px / p.entry_price - 1),
                "bars_held": p.bars, "exit_reason": reason,
                "mae_r": p.mae / p.stop_dist if p.stop_dist else np.nan,
                "mfe_r": p.mfe / p.stop_dist if p.stop_dist else np.nan,
            })
            p.qty -= qty
            p.costs -= entry_cost_share
            if p.qty <= cfg.min_qty:
                del pos[p.symbol]

        for ts in timeline:
            todays = []
            for sym in frames:
                i = ptr[sym]
                if i < len(IDX[sym]) and IDX[sym][i] == ts:
                    todays.append((sym, i))
                    ptr[sym] = i + 1
            # ---------------------------------------------------------- opens
            for sym, i in todays:
                b = B[sym]
                o, h, l, c, v = b["open"][i], b["high"][i], b["low"][i], b["close"][i], b["volume"][i]
                cap_qty = v * cm.max_volume_participation if v > 0 else 0.0
                p = pos.get(sym)
                # pending market exit -> fill at open
                if p is not None and p.pending_exit:
                    q = min(p.pending_exit_qty, cap_qty) if cap_qty > 0 else 0.0
                    if q > 0:
                        reason = p.pending_exit
                        close_qty(p, q, ts, o, reason)
                        if sym in pos:
                            pos[sym].pending_exit_qty -= q
                    p = pos.get(sym)
                # pending entry -> fill at open
                if sym in pending_entries and sym not in pos:
                    e = pending_entries.pop(sym)
                    d = e["direction"]
                    px = cm.fill_price(o, d)
                    eq = equity_now() if pos else cash
                    qty = e["qty"]
                    gross_now = sum(pp.qty * last_close[pp.symbol] for pp in pos.values())
                    room = max(cfg.max_gross_exposure * eq - gross_now, 0.0) / px
                    affordable = (cash / (px * (1 + cm.commission_pct) + cm.commission_per_share)) if d > 0 else room
                    qty = min(qty, room, affordable, cap_qty)
                    if not cfg.allow_fractional:
                        qty = np.floor(qty)
                    if qty < max(cfg.min_qty, 1e-12) or qty <= 0:
                        rejected.append({"ts": ts, "symbol": sym, "reason": "size<min after cash/volume/exposure limits"})
                    else:
                        comm = cm.commission(qty, px)
                        cash -= d * qty * px + comm
                        sd = e["stop_dist"]
                        stop = px - d * sd
                        target = px + d * e["tp_dist"] if np.isfinite(e["tp_dist"]) else None
                        pos[sym] = _Pos(sym, d, qty, ts, px, stop, target, sd, qty * sd, comm)
                        last_close[sym] = px
                        if qty < e["qty"] - 1e-9:
                            rejected.append({"ts": ts, "symbol": sym, "reason": "partial fill",
                                             "requested": e["qty"], "filled": qty})
                p = pos.get(sym)
                # ---------------------------------------------- intrabar stop / target
                if p is not None:
                    d = p.direction
                    p.mae = max(p.mae, (p.entry_price - l) if d > 0 else (h - p.entry_price))
                    p.mfe = max(p.mfe, (h - p.entry_price) if d > 0 else (p.entry_price - l))
                    gapped_stop = (o <= p.stop) if d > 0 else (o >= p.stop)
                    hit_stop = (l <= p.stop) if d > 0 else (h >= p.stop)
                    gapped_tp = p.target is not None and ((o >= p.target) if d > 0 else (o <= p.target))
                    hit_tp = p.target is not None and ((h >= p.target) if d > 0 else (l <= p.target))
                    entered_now = p.entry_ts == ts
                    if gapped_stop and not entered_now:
                        close_qty(p, p.qty, ts, o, "stop_gap")
                    elif hit_stop:
                        close_qty(p, p.qty, ts, p.stop, "stop")
                    elif gapped_tp and not entered_now:
                        close_qty(p, p.qty, ts, o, "target_gap")
                    elif hit_tp:
                        close_qty(p, p.qty, ts, p.target, "target")
                last_close[sym] = c
            # ---------------------------------------------------------- closes
            for sym, i in todays:
                p = pos.get(sym)
                s = {k: col[i] for k, col in SIG[sym].items()}
                if p is None:
                    continue
                p.bars += 1
                if p.direction < 0 and borrow_per_bar:
                    fee = p.qty * last_close[sym] * borrow_per_bar
                    cash -= fee
                    p.costs += fee
                if p.pending_exit:
                    continue
                reason = None
                if (p.direction > 0 and s["long_exit"]) or (p.direction < 0 and s["short_exit"]):
                    reason = "signal_exit"
                elif (p.direction > 0 and s["short_entry"]) or (p.direction < 0 and s["long_entry"]):
                    reason = "reversal"
                elif strategy.max_holding_bars is not None and p.bars >= int(strategy.resolve(strategy.max_holding_bars)):
                    reason = "time_stop"
                if reason:
                    p.pending_exit, p.pending_exit_qty = reason, p.qty
                elif strategy.stop.trailing and np.isfinite(s["stop_dist"]):
                    c = last_close[sym]
                    p.stop = max(p.stop, c - s["stop_dist"]) if p.direction > 0 else min(p.stop, c + s["stop_dist"])

            eq = equity_now()
            # ---------------------------------------------------------- new entries (decided at close)
            slots = cfg.max_positions - len(pos) - len(pending_entries)
            cands = []
            for sym, i in todays:
                if sym in pos or sym in pending_entries:
                    continue
                s = {k: col[i] for k, col in SIG[sym].items()}
                if s["long_entry"] or s["short_entry"]:
                    cands.append((-(s["rank"] if np.isfinite(s["rank"]) else -np.inf), sym, s))
            cands.sort(key=lambda x: (x[0], x[1]))  # deterministic: rank desc, then symbol
            for _, sym, s in cands:
                if slots <= 0:
                    rejected.append({"ts": ts, "symbol": sym, "reason": "max_positions"})
                    continue
                d = 1 if s["long_entry"] else -1
                c = last_close[sym]
                risk_qty = cfg.risk_per_trade * eq / s["stop_dist"]
                cap_qty = cfg.max_position_pct * eq / c
                pending_entries[sym] = {"direction": d, "qty": min(risk_qty, cap_qty),
                                        "stop_dist": float(s["stop_dist"]), "tp_dist": float(s["tp_dist"])}
                slots -= 1

            gross = sum(p.qty * last_close[p.symbol] for p in pos.values())
            eq_rows.append({"ts": ts, "equity": eq, "cash": cash, "gross_exposure": gross / eq if eq > 0 else np.nan,
                            "n_positions": len(pos)})

        # close remaining at last known close (marked, with exit costs)
        last_ts = timeline[-1] if timeline else None
        for p in list(pos.values()):
            close_qty(p, p.qty, last_ts, last_close[p.symbol], "end_of_data")
        if eq_rows:
            eq_rows[-1]["equity"] = cash
            eq_rows[-1]["cash"] = cash
            eq_rows[-1]["gross_exposure"] = 0.0
            eq_rows[-1]["n_positions"] = 0

        equity = pd.DataFrame(eq_rows).set_index("ts") if eq_rows else pd.DataFrame(
            columns=["equity", "cash", "gross_exposure", "n_positions"])
        return BacktestResult(pd.DataFrame(trades), equity, cfg.to_dict(), strategy.version_id, rejected)
