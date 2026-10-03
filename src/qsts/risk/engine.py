"""Risk Engine + position sizing.

Every proposed trade passes through `RiskEngine.evaluate`, which either returns an approved size
or NO_TRADE with explicit reasons. Rules are evaluated independently so the log shows every reason
a trade was blocked, not just the first.

All limits are configuration (RiskLimits), not magic numbers; defaults are conservative
starting points and are documented inline. `MICRO_LIVE` is the strict preset for the first
real-money stage (~EUR 100).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

import numpy as np
import pandas as pd

from qsts.core.kill_switch import KillSwitch


class SizeTier(str, Enum):
    NO_TRADE = "NO_TRADE"
    SMALL = "SMALL"
    NORMAL = "NORMAL"
    HIGH_CONVICTION = "HIGH_CONVICTION"


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade: float = 0.01  # equity fraction lost at stop (1% ~ standard swing convention)
    max_risk_per_trade: float = 0.02  # hard cap even for high conviction
    max_total_open_risk: float = 0.06  # sum of (entry-stop)*qty over open positions / equity
    max_position_pct: float = 0.20
    max_positions: int = 10
    max_sector_exposure: float = 0.40  # gross notional per sector / equity
    max_correlated_risk: float = 0.03  # open risk in positions with corr >= threshold to the candidate
    correlation_threshold: float = 0.7
    max_gross_exposure: float = 1.0  # no leverage by default
    max_short_exposure: float = 0.30
    allow_short: bool = True
    daily_loss_limit: float = 0.03
    weekly_loss_limit: float = 0.06
    max_consecutive_losses: int = 5  # pause new entries after this many losses in a row
    # Drawdown de-risking: risk multiplier falls linearly from 1 at dd_start to 0 at dd_stop.
    dd_start: float = 0.05
    dd_stop: float = 0.20
    max_adv_participation: float = 0.01  # order notional / average daily dollar volume
    max_spread_bps: float = 30.0
    min_days_to_earnings: int = 2
    min_order_notional: float = 1.0  # broker minimum (configure per broker)
    allow_fractional: bool = True
    min_strategy_health: float = 0.5  # 0..1 from decay monitor; below -> no trade
    # Calibrated-confidence tiers (only used when the signal carries a CALIBRATED confidence)
    small_below: float = 0.5
    high_conviction_above: float = 0.65
    high_conviction_multiplier: float = 1.5
    small_multiplier: float = 0.5


MICRO_LIVE = RiskLimits(
    risk_per_trade=0.01, max_risk_per_trade=0.01, max_total_open_risk=0.03, max_position_pct=0.5,
    max_positions=2, max_sector_exposure=0.5, max_gross_exposure=1.0, allow_short=False,
    max_short_exposure=0.0, daily_loss_limit=0.02, weekly_loss_limit=0.05, max_consecutive_losses=3,
    high_conviction_multiplier=1.0,
)


@dataclass
class OpenPosition:
    symbol: str
    direction: int  # +1 long, -1 short
    qty: float
    price: float  # current mark
    stop: float
    sector: str | None = None
    strategy_id: str | None = None

    @property
    def notional(self) -> float:
        return abs(self.qty * self.price)

    @property
    def open_risk(self) -> float:
        return max(self.direction * (self.price - self.stop), 0.0) * self.qty


@dataclass
class PortfolioState:
    equity: float
    cash: float
    peak_equity: float
    day_start_equity: float
    week_start_equity: float
    consecutive_losses: int = 0
    positions: list[OpenPosition] = field(default_factory=list)

    @property
    def drawdown(self) -> float:
        return self.equity / self.peak_equity - 1 if self.peak_equity > 0 else 0.0


@dataclass
class TradeIntent:
    symbol: str
    direction: int
    entry: float
    stop: float
    sector: str | None = None
    strategy_id: str | None = None
    strategy_health: float = 1.0
    calibrated_confidence: float | None = None  # None = no calibration evidence -> no conviction sizing
    avg_daily_dollar_volume: float | None = None
    spread_bps: float | None = None
    shortable: bool | None = None  # None = unknown -> treated as NOT shortable
    days_to_earnings: int | None = None
    data_valid: bool = True
    broker_available: bool = True
    regime_compatible: bool = True


@dataclass
class RiskDecision:
    approved: bool
    tier: SizeTier
    qty: float = 0.0
    risk_amount: float = 0.0
    notional: float = 0.0
    reasons: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)


class RiskEngine:
    def __init__(self, limits: RiskLimits = RiskLimits(), kill_switch: KillSwitch | None = None):
        self.limits = limits
        self.kill_switch = kill_switch

    def drawdown_multiplier(self, dd: float) -> float:
        L = self.limits
        x = -dd
        if x <= L.dd_start:
            return 1.0
        if x >= L.dd_stop:
            return 0.0
        return 1.0 - (x - L.dd_start) / (L.dd_stop - L.dd_start)

    def _tier(self, conf: float | None) -> tuple[SizeTier, float]:
        L = self.limits
        if conf is None:
            return SizeTier.NORMAL, 1.0
        if conf < L.small_below:
            return SizeTier.SMALL, L.small_multiplier
        if conf > L.high_conviction_above:
            return SizeTier.HIGH_CONVICTION, L.high_conviction_multiplier
        return SizeTier.NORMAL, 1.0

    def evaluate(self, t: TradeIntent, p: PortfolioState, returns: pd.DataFrame | None = None) -> RiskDecision:
        """`returns`: trailing daily returns (columns = symbols) known at decision time, for correlation."""
        L = self.limits
        R: list[str] = []
        adj: list[str] = []

        # ---------------------------------------------------------------- hard blocks
        if self.kill_switch is not None and not self.kill_switch.trading_allowed():
            R.append("kill switch engaged")
        if not t.broker_available:
            R.append("broker unavailable")
        if not t.data_valid:
            R.append("data invalid/incomplete")
        if not t.regime_compatible:
            R.append("regime incompatible")
        if t.strategy_health < L.min_strategy_health:
            R.append(f"strategy health {t.strategy_health:.2f} < {L.min_strategy_health}")
        stop_dist = t.direction * (t.entry - t.stop)
        if not stop_dist > 0:
            R.append("stop on wrong side of entry")
        if t.direction < 0:
            if not L.allow_short:
                R.append("shorting disabled")
            if t.shortable is not True:
                R.append("short availability not confirmed by broker")
        if t.spread_bps is not None and t.spread_bps > L.max_spread_bps:
            R.append(f"spread {t.spread_bps:.1f}bps > {L.max_spread_bps}")
        if t.days_to_earnings is not None and 0 <= t.days_to_earnings < L.min_days_to_earnings:
            R.append(f"earnings in {t.days_to_earnings} days")
        if any(x.symbol == t.symbol for x in p.positions):
            R.append("already positioned in symbol")
        if len(p.positions) >= L.max_positions:
            R.append("max positions reached")
        if p.equity / p.day_start_equity - 1 <= -L.daily_loss_limit:
            R.append("daily loss limit hit")
        if p.equity / p.week_start_equity - 1 <= -L.weekly_loss_limit:
            R.append("weekly loss limit hit")
        if p.consecutive_losses >= L.max_consecutive_losses:
            R.append(f"{p.consecutive_losses} consecutive losses")
        ddm = self.drawdown_multiplier(p.drawdown)
        if ddm <= 0:
            R.append(f"drawdown {p.drawdown:.1%} beyond de-risk stop")
        if R:
            return RiskDecision(False, SizeTier.NO_TRADE, reasons=R)

        # ---------------------------------------------------------------- sizing
        tier, mult = self._tier(t.calibrated_confidence)
        risk_frac = min(L.risk_per_trade * mult * ddm, L.max_risk_per_trade)
        if ddm < 1:
            adj.append(f"drawdown multiplier {ddm:.2f}")
        h_mult = min(max(t.strategy_health, 0.0), 1.0)
        if h_mult < 1:
            risk_frac *= h_mult
            adj.append(f"strategy health multiplier {h_mult:.2f}")
        risk_amt = risk_frac * p.equity
        qty = risk_amt / stop_dist

        def cap(q, limit_qty, why):
            if limit_qty < q:
                adj.append(why)
                return max(limit_qty, 0.0)
            return q

        qty = cap(qty, L.max_position_pct * p.equity / t.entry, "max position size")
        open_risk = sum(x.open_risk for x in p.positions)
        qty = cap(qty, (L.max_total_open_risk * p.equity - open_risk) / stop_dist, "total open risk")
        gross = sum(x.notional for x in p.positions)
        qty = cap(qty, (L.max_gross_exposure * p.equity - gross) / t.entry, "gross exposure")
        if t.direction > 0:
            qty = cap(qty, p.cash / t.entry, "available cash")
        else:
            short = sum(x.notional for x in p.positions if x.direction < 0)
            qty = cap(qty, (L.max_short_exposure * p.equity - short) / t.entry, "short exposure")
        if t.sector:
            sec = sum(x.notional for x in p.positions if x.sector == t.sector)
            qty = cap(qty, (L.max_sector_exposure * p.equity - sec) / t.entry, f"sector exposure ({t.sector})")
        if returns is not None and p.positions and t.symbol in returns.columns:
            corr = returns.corr()[t.symbol]
            linked = [x for x in p.positions if x.symbol in corr.index and corr[x.symbol] >= L.correlation_threshold]
            linked_risk = sum(x.open_risk for x in linked)
            qty = cap(qty, (L.max_correlated_risk * p.equity - linked_risk) / stop_dist,
                      f"correlated exposure with {[x.symbol for x in linked]}")
        if t.avg_daily_dollar_volume:
            qty = cap(qty, L.max_adv_participation * t.avg_daily_dollar_volume / t.entry, "liquidity (ADV)")
        if not L.allow_fractional:
            qty = float(np.floor(qty))
        notional = qty * t.entry
        if qty <= 0 or notional < L.min_order_notional:
            return RiskDecision(False, SizeTier.NO_TRADE, reasons=[f"size too small after limits ({notional:.2f})"],
                                adjustments=adj)
        if tier is SizeTier.NORMAL and qty * stop_dist < 0.75 * L.risk_per_trade * p.equity:
            tier = SizeTier.SMALL
        return RiskDecision(True, tier, qty, qty * stop_dist, notional, [], adj)

    def with_limits(self, **kw) -> "RiskEngine":
        return RiskEngine(replace(self.limits, **kw), self.kill_switch)
