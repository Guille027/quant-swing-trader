"""Market scanner: one analysis cycle.

universe -> validate data (as of T) -> features/strategies -> regime -> candidates -> risk -> signals.
Every bar used has available_at <= T, so a scan at T can be replayed later and see exactly what
the system saw ("reconstruct what the system saw at that moment").
NO TRADE is a normal, frequent outcome.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from qsts.data.bars import Timeframe
from qsts.data.quality import DataQualityError, QualityConfig, validate_and_clean
from qsts.risk.engine import PortfolioState, RiskEngine, TradeIntent
from qsts.strategy.definition import CompiledStrategy, StrategyDefinition
from qsts.strategy.regime import RegimeConfig, classify

TRADABLE_STATUSES = {"PAPER", "APPROVED", "ACTIVE"}


@dataclass
class StrategySlot:
    strategy_id: str
    sd: StrategyDefinition
    status: str
    health: float = 1.0


@dataclass
class ScanSignal:
    symbol: str
    strategy_id: str
    direction: str  # LONG | SHORT | NO_TRADE
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    rr: float | None = None
    qty: float | None = None
    risk_amount: float | None = None
    tier: str | None = None
    confidence: float | None = None  # only ever a CALIBRATED value; None otherwise
    reasons: list[str] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)


@dataclass
class ScanReport:
    asof: pd.Timestamp
    regime: dict
    assets_scanned: int = 0
    valid_assets: int = 0
    invalid: dict[str, str] = field(default_factory=dict)
    potential_setups: int = 0
    signals: list[ScanSignal] = field(default_factory=list)
    no_trade: list[ScanSignal] = field(default_factory=list)

    @property
    def final_signals(self) -> int:
        return len(self.signals)


class MarketScanner:
    def __init__(self, load_bars: Callable[[str], pd.DataFrame], strategies: list[StrategySlot], risk: RiskEngine,
                 benchmark: str = "SPY", quality: QualityConfig = QualityConfig(), regime_cfg: RegimeConfig = RegimeConfig(),
                 sectors: dict[str, str] | None = None, calibrators: dict | None = None):
        self.load_bars, self.strategies, self.risk = load_bars, strategies, risk
        self.benchmark, self.quality, self.regime_cfg = benchmark, quality, regime_cfg
        self.sectors = sectors or {}
        self.calibrators = calibrators or {}

    def _asof(self, df: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
        if "available_at" in df:
            return df[df["available_at"] <= asof]
        return df

    def scan(self, universe: list[str], asof, portfolio: PortfolioState, returns: pd.DataFrame | None = None) -> ScanReport:
        asof = pd.Timestamp(asof)
        asof = asof.tz_localize("UTC") if asof.tz is None else asof
        regime_now: dict = {}
        regime_series = None
        try:
            bvb = validate_and_clean(self.load_bars(self.benchmark), self.benchmark, Timeframe.D1, self.quality)
            bdf = self._asof(bvb.df, asof)
            reg = classify(bdf, self.regime_cfg)
            regime_series = reg["trend"]
            last = reg.iloc[-1]
            regime_now = {"trend": last["trend"], "volatility": last["volatility"], "asof_bar": str(reg.index[-1])}
        except (DataQualityError, KeyError, IndexError, FileNotFoundError) as e:
            regime_now = {"error": f"benchmark unavailable: {e}"}
        rep = ScanReport(asof, regime_now, assets_scanned=len(universe))
        tradable = [s for s in self.strategies if s.status in TRADABLE_STATUSES]
        for sym in universe:
            try:
                raw = self.load_bars(sym)
                vb = validate_and_clean(raw[raw.index <= asof], sym, Timeframe.D1, self.quality, asof=asof)
            except DataQualityError as e:
                rep.invalid[sym] = "; ".join(f"{i.code}" for i in e.report.issues if i.severity.value == "ERROR")
                continue
            except Exception as e:  # noqa: BLE001 - one bad symbol must not stop the scan
                rep.invalid[sym] = repr(e)
                continue
            df = self._asof(vb.df, asof)
            if len(df) < 2:
                rep.invalid[sym] = "insufficient history as of scan time"
                continue
            rep.valid_assets += 1
            for slot in tradable:
                if slot.sd.allowed_regimes is not None and regime_series is None:
                    rep.no_trade.append(ScanSignal(sym, slot.strategy_id, "NO_TRADE", reasons=["regime unknown"]))
                    continue
                reg = regime_series.reindex(df.index) if slot.sd.allowed_regimes is not None else None
                ev = CompiledStrategy(slot.sd).evaluate(df, reg)
                row = ev.iloc[-1]
                if not (row["long_entry"] or row["short_entry"]):
                    continue
                rep.potential_setups += 1
                d = 1 if row["long_entry"] else -1
                c = float(df["close"].iloc[-1])
                stop = c - d * float(row["stop_dist"])
                tgt = c + d * float(row["tp_dist"]) if pd.notna(row["tp_dist"]) else None
                cal = self.calibrators.get(slot.strategy_id)
                conf = cal.predict(float(row["rank"])) if cal is not None else None
                intent = TradeIntent(symbol=sym, direction=d, entry=c, stop=stop, sector=self.sectors.get(sym),
                                     strategy_id=slot.strategy_id, strategy_health=slot.health,
                                     calibrated_confidence=conf, shortable=None if d < 0 else True)
                dec = self.risk.evaluate(intent, portfolio, returns)
                snap = {"bar_ts": str(df.index[-1]), "available_at": str(df["available_at"].iloc[-1]),
                        "close": c, "stop_dist": float(row["stop_dist"]), "data_version": vb.version,
                        "strategy_version": slot.sd.version_id, "regime": regime_now}
                s = ScanSignal(sym, slot.strategy_id, "LONG" if d > 0 else "SHORT", c, stop, tgt,
                               (abs(tgt - c) / abs(c - stop)) if tgt else None, dec.qty, dec.risk_amount, dec.tier.value,
                               conf, dec.reasons + dec.adjustments, snap)
                if dec.approved:
                    rep.signals.append(s)
                else:
                    s.direction = "NO_TRADE"
                    rep.no_trade.append(s)
        return rep


def render_report(rep: ScanReport, portfolio: PortfolioState, counts: dict[str, int], system: dict) -> str:
    """Plain-text dashboard (the 'what success looks like' screen)."""
    L = ["=" * 50, "QUANT TRADING SYSTEM".center(50), "=" * 50, "", "MARKET", "S&P 500",
         f"Regime: {rep.regime.get('trend', 'UNKNOWN')}", f"Volatility: {rep.regime.get('volatility', 'UNKNOWN')}",
         "", "-" * 50, "", "SCAN " + str(rep.asof), "", f"Assets scanned: {rep.assets_scanned}",
         f"Valid assets: {rep.valid_assets}", f"Potential setups: {rep.potential_setups}",
         f"Final signals: {rep.final_signals}", "", "-" * 50, "", "SIGNALS", ""]
    if not rep.signals:
        L.append("NO TRADE — no opportunity passed all checks")
    for s in rep.signals:
        L += [s.symbol, s.direction,
              f"Confidence: {s.confidence:.0%}" if s.confidence is not None else "Confidence: n/a (not calibrated)",
              f"Strategy: {s.strategy_id}", f"Entry: {s.entry:.2f}", f"Stop: {s.stop:.2f}",
              f"Target: {s.target:.2f}" if s.target else "Target: dynamic exit", f"Risk: {s.risk_amount:.2f}",
              f"R:R: {s.rr:.2f}" if s.rr else "R:R: n/a", ""]
    for s in rep.no_trade[:10]:
        L += [s.symbol, "NO TRADE", f"Reason: {'; '.join(s.reasons[:2])}", ""]
    eq = portfolio.equity
    expo = sum(p.notional for p in portfolio.positions) / eq if eq else 0
    L += ["-" * 50, "", "PORTFOLIO", "", f"Capital: {eq:,.2f}", f"Cash: {portfolio.cash:,.2f}",
          f"Exposure: {expo:.0%}", f"Drawdown: {portfolio.drawdown:.1%}", "", "-" * 50, "", "STRATEGIES", ""]
    L += [f"{k.title().replace('_', ' ')}: {v}" for k, v in counts.items()]
    L += ["", "-" * 50, "", "SYSTEM", ""] + [f"{k}: {v}" for k, v in system.items()] + ["", "=" * 50]
    return "\n".join(L)
