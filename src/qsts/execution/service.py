"""Execution service: one code path for PAPER, MANUAL, SEMI-AUTO and LIVE.

signal -> kill switch -> broker connectivity -> reconciliation state -> live safety gate ->
instrument checks -> risk engine -> mode policy (queue for approval / auto submit) -> broker.

Connection failure policy: log + notify, never close/modify/open positions, block new orders,
and require reconciliation of local vs broker state after reconnecting before trading resumes.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from qsts.config import Settings
from qsts.core.hashing import hash_obj
from qsts.core.kill_switch import KillSwitch
from qsts.core.logging import get_logger
from qsts.core.modes import ModeController, SystemMode
from qsts.execution.broker import (BrokerAdapter, BrokerError, BrokerUnavailable, OrderRequest, OrderState, OrderStatus,
                                   OrderType, Side)
from qsts.notify.service import Event, NotificationService
from qsts.risk.engine import MICRO_LIVE, PortfolioState, RiskDecision, RiskEngine, SizeTier, TradeIntent

log = get_logger("execution")


@dataclass
class Signal:
    symbol: str
    direction: int
    entry: float
    stop: float
    target: float | None
    strategy_id: str
    strategy_status: str
    asof: pd.Timestamp
    intent_extra: dict = field(default_factory=dict)  # fields forwarded to TradeIntent (sector, adv, ...)

    @property
    def key(self) -> str:
        return hash_obj({"s": self.symbol, "d": self.direction, "st": self.strategy_id, "t": str(self.asof)}, 24)


@dataclass
class Outcome:
    status: str  # OBSERVED | BLOCKED | NO_TRADE | PENDING_APPROVAL | SUBMITTED | REJECTED_BY_BROKER
    reasons: list[str] = field(default_factory=list)
    decision: RiskDecision | None = None
    order: OrderState | None = None
    signal_key: str | None = None


# ============================================================== live safety (phase 20)
@dataclass
class LiveSafetyGate:
    """All must hold for every LIVE order. Nothing here can be satisfied automatically by the system:
    `user_confirmed_session` is set only by an explicit user action for the current session."""
    settings: Settings
    kill_switch: KillSwitch
    paper_report_ids: dict[str, str] = field(default_factory=dict)  # strategy_id -> passed paper report
    user_confirmed_session: bool = False
    data_valid: bool = True
    critical_errors: int = 0
    micro_live: bool = True
    micro_live_max_capital: float = 100.0

    def kill_switch_operational(self) -> bool:
        try:
            d = self.kill_switch.path.parent
            d.mkdir(parents=True, exist_ok=True)
            fd, p = tempfile.mkstemp(dir=d)
            os.close(fd)
            os.unlink(p)
            return True
        except OSError:
            return False

    def check(self, *, broker: BrokerAdapter, mode: SystemMode, risk: RiskEngine | None, strategy_id: str,
              strategy_status: str, equity: float) -> list[str]:
        f = []
        if not self.settings.live_allowed_by_config:
            f.append("live trading disabled in configuration")
        if broker.environment != "live":
            f.append("broker is not a live adapter")
        if mode < SystemMode.MANUAL_APPROVAL:
            f.append(f"mode {mode.name} does not permit real money")
        if not self.user_confirmed_session:
            f.append("no explicit user confirmation for this live session")
        if risk is None:
            f.append("risk engine inactive")
        if not self.kill_switch_operational():
            f.append("kill switch not operational")
        if strategy_status not in ("APPROVED", "ACTIVE"):
            f.append(f"strategy status {strategy_status} not approved")
        if strategy_id not in self.paper_report_ids:
            f.append("strategy has no passed paper-trading report")
        if not self.data_valid:
            f.append("market data not valid")
        if self.critical_errors:
            f.append(f"{self.critical_errors} unresolved critical errors")
        if not broker.is_connected():
            f.append("broker not connected")
        if self.micro_live:
            if risk is not None and risk.limits != MICRO_LIVE:
                f.append("micro-live requires MICRO_LIVE risk limits")
            if equity > self.micro_live_max_capital * 1.05:
                f.append(f"equity {equity:.2f} above micro-live cap {self.micro_live_max_capital}")
        return f


# ============================================================== service
class ExecutionService:
    def __init__(self, broker: BrokerAdapter, risk: RiskEngine, kill_switch: KillSwitch, modes: ModeController,
                 notifier: NotificationService, live_gate: LiveSafetyGate | None = None,
                 semi_auto_max_risk_frac: float = 0.005):
        self.broker, self.risk, self.ks, self.modes, self.notify = broker, risk, kill_switch, modes, notifier
        self.live_gate = live_gate
        # SEMI_AUTOMATIC: orders risking more than this fraction of equity (or HIGH_CONVICTION) need approval.
        self.semi_auto_max_risk_frac = semi_auto_max_risk_frac
        self.connected = True
        self.needs_reconcile = False
        self.expected_positions: dict[str, float] = {}
        self.pending: dict[str, tuple[Signal, RiskDecision]] = {}
        self.submitted: dict[str, OrderState] = {}
        self.journal: list[dict] = []  # full decision trail (persisted by caller)
        self._seen_fill: dict[str, float] = {}

    # -------------------------------------------------------------- helpers
    def _record(self, sig: Signal | None, outcome: Outcome) -> Outcome:
        self.journal.append({"ts": datetime.now(timezone.utc).isoformat(), "signal": sig.__dict__ if sig else None,
                             "status": outcome.status, "reasons": outcome.reasons,
                             "qty": outcome.decision.qty if outcome.decision else None})
        return outcome

    def _on_disconnect(self, err: Exception) -> None:
        if self.connected:
            self.connected = False
            self.needs_reconcile = True
            log.error("broker connection lost: %r", err)
            self.notify.notify(Event.CONNECTION_LOST, f"{self.broker.name} unreachable",
                               "New orders blocked. Positions NOT modified. Reconciliation required on reconnect.")

    def heartbeat(self) -> bool:
        try:
            self.broker.account()
        except BrokerUnavailable as e:
            self._on_disconnect(e)
            return False
        if not self.connected:
            self.connected = True
            self.notify.notify(Event.CONNECTION_RESTORED, f"{self.broker.name} reachable again", "Reconciling…")
            self.reconcile()
        return True

    def reconcile(self) -> dict:
        """Compare locally expected positions with the broker's. Broker is the source of truth;
        any divergence keeps trading blocked until a user explicitly accepts the broker state."""
        try:
            actual = self.broker.positions()
        except BrokerUnavailable as e:
            self._on_disconnect(e)
            return {"ok": False, "reason": "broker unavailable"}
        syms = set(actual) | set(self.expected_positions)
        diff = {s: (self.expected_positions.get(s, 0.0), actual.get(s, 0.0)) for s in syms
                if abs(self.expected_positions.get(s, 0.0) - actual.get(s, 0.0)) > 1e-9}
        if diff:
            self.needs_reconcile = True
            self.notify.notify(Event.RECONCILIATION_MISMATCH, "Local and broker positions differ", str(diff), diff=diff)
            return {"ok": False, "diff": diff}
        self.needs_reconcile = False
        return {"ok": True, "diff": {}}

    def accept_broker_state(self, *, confirmed_by_user: bool) -> None:
        if not confirmed_by_user:
            raise PermissionError("accepting broker state requires explicit user confirmation")
        self.expected_positions = dict(self.broker.positions())
        self.needs_reconcile = False

    def sync_fills(self) -> None:
        """Apply new fills of orders WE submitted to the expected positions (incremental)."""
        try:
            orders = self.broker.orders()
        except BrokerUnavailable as e:
            self._on_disconnect(e)
            return
        for cid in self.submitted:
            st = orders.get(cid)
            if st is None:
                continue
            delta = st.filled_qty - self._seen_fill.get(cid, 0.0)
            if delta:
                sgn = 1 if st.request.side is Side.BUY else -1
                sym = st.request.symbol
                self.expected_positions[sym] = self.expected_positions.get(sym, 0.0) + sgn * delta
                self._seen_fill[cid] = st.filled_qty

    # -------------------------------------------------------------- main entry
    def handle_signal(self, sig: Signal, pstate: PortfolioState, returns: pd.DataFrame | None = None) -> Outcome:
        mode = self.modes.mode
        if mode < SystemMode.PAPER:
            return self._record(sig, Outcome("OBSERVED", ["observation/backtest mode: no execution"], signal_key=sig.key))
        if not self.ks.trading_allowed():
            return self._record(sig, Outcome("BLOCKED", ["kill switch engaged"], signal_key=sig.key))
        if not self.heartbeat():
            return self._record(sig, Outcome("BLOCKED", ["broker unavailable"], signal_key=sig.key))
        if self.needs_reconcile:
            return self._record(sig, Outcome("BLOCKED", ["reconciliation pending"], signal_key=sig.key))
        if sig.key in self.pending or f"qsts-{sig.key}" in self.submitted:
            return self._record(sig, Outcome("BLOCKED", ["duplicate signal"], signal_key=sig.key))
        if self.broker.environment == "live":
            if self.live_gate is None:
                return self._record(sig, Outcome("BLOCKED", ["no live safety gate configured"], signal_key=sig.key))
            fails = self.live_gate.check(broker=self.broker, mode=mode, risk=self.risk, strategy_id=sig.strategy_id,
                                         strategy_status=sig.strategy_status, equity=pstate.equity)
            if fails:
                return self._record(sig, Outcome("BLOCKED", fails, signal_key=sig.key))
        info = self.broker.instrument(sig.symbol)
        if not info.tradable:
            return self._record(sig, Outcome("NO_TRADE", ["instrument not tradable at broker"], signal_key=sig.key))
        intent = TradeIntent(symbol=sig.symbol, direction=sig.direction, entry=sig.entry, stop=sig.stop,
                             strategy_id=sig.strategy_id, shortable=info.shortable, broker_available=True,
                             **sig.intent_extra)
        risk = self.risk if info.fractional else self.risk.with_limits(allow_fractional=False)
        dec = risk.evaluate(intent, pstate, returns)
        if not dec.approved:
            return self._record(sig, Outcome("NO_TRADE", dec.reasons, dec, signal_key=sig.key))
        needs_approval = mode is SystemMode.MANUAL_APPROVAL or (
            mode is SystemMode.SEMI_AUTOMATIC and (dec.tier is SizeTier.HIGH_CONVICTION
                                                   or dec.risk_amount > self.semi_auto_max_risk_frac * pstate.equity))
        if needs_approval:
            self.pending[sig.key] = (sig, dec)
            self.notify.notify(Event.NEW_SIGNAL, f"{sig.symbol} {'LONG' if sig.direction > 0 else 'SHORT'} awaiting approval",
                               f"qty={dec.qty:.4f} entry~{sig.entry} stop={sig.stop} risk={dec.risk_amount:.2f}")
            return self._record(sig, Outcome("PENDING_APPROVAL", [], dec, signal_key=sig.key))
        return self._record(sig, self._submit(sig, dec))

    def approve(self, key: str, *, actor: str) -> Outcome:
        if actor in ("ai", "system", "evolution"):
            raise PermissionError("approval requires a human")
        sig, dec = self.pending.pop(key)
        if not self.ks.trading_allowed():
            return self._record(sig, Outcome("BLOCKED", ["kill switch engaged"], dec, signal_key=key))
        if not self.heartbeat() or self.needs_reconcile:
            self.pending[key] = (sig, dec)
            return self._record(sig, Outcome("BLOCKED", ["broker unavailable or reconciliation pending"], dec, signal_key=key))
        self.notify.notify(Event.TRADE_APPROVED, f"{sig.symbol} approved by {actor}")
        return self._record(sig, self._submit(sig, dec))

    def reject(self, key: str, *, actor: str, reason: str = "") -> None:
        sig, dec = self.pending.pop(key)
        self._record(sig, Outcome("NO_TRADE", [f"rejected by {actor}: {reason}"], dec, signal_key=key))

    def _submit(self, sig: Signal, dec: RiskDecision) -> Outcome:
        req = OrderRequest(client_order_id=f"qsts-{sig.key}", symbol=sig.symbol,
                           side=Side.BUY if sig.direction > 0 else Side.SELL, qty=dec.qty, order_type=OrderType.MARKET)
        try:
            st = self.broker.submit(req)
        except BrokerUnavailable as e:
            self._on_disconnect(e)
            return Outcome("BLOCKED", ["broker became unavailable during submission"], dec, signal_key=sig.key)
        except BrokerError as e:
            self.notify.notify(Event.BROKER_ERROR, f"order for {sig.symbol} failed", repr(e))
            return Outcome("REJECTED_BY_BROKER", [repr(e)], dec, signal_key=sig.key)
        if st.status is OrderStatus.REJECTED:
            self.notify.notify(Event.BROKER_ERROR, f"order for {sig.symbol} rejected", st.reject_reason or "")
            return Outcome("REJECTED_BY_BROKER", [st.reject_reason or "rejected"], dec, st, sig.key)
        self.submitted[req.client_order_id] = st
        self.notify.notify(Event.TRADE_EXECUTED, f"{sig.symbol} order submitted", f"qty={dec.qty:.4f}")
        return Outcome("SUBMITTED", [], dec, st, sig.key)

    # -------------------------------------------------------------- kill switch
    def stop_all_trading(self, reason: str) -> None:
        """Engage kill switch and cancel working ENTRY orders. Positions are kept;
        closing them requires a separate explicit user action."""
        self.ks.engage(reason)
        self.notify.notify(Event.KILL_SWITCH, "STOP ALL TRADING", reason)
        self.pending.clear()
        try:
            for cid, st in self.broker.orders().items():
                if cid in self.submitted and st.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                    self.broker.cancel(cid)
        except BrokerError as e:  # kill switch must still hold even if the broker is down
            log.error("could not cancel orders during kill switch: %r", e)
