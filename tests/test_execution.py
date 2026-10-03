import smtplib

import pandas as pd
import pytest

from qsts.backtest.engine import CostModel
from qsts.config import Settings
from qsts.core.kill_switch import KillSwitch
from qsts.core.modes import ModeController, SystemMode
from qsts.execution.broker import (BrokerAdapter, InstrumentInfo, OrderRequest, OrderStatus, OrderType, PaperBroker,
                                   Side)
from qsts.execution.service import ExecutionService, LiveSafetyGate, Signal
from qsts.notify.service import (Channel, EmailChannel, Event, LogChannel, NotificationService, WhatsAppChannel)
from qsts.risk.engine import MICRO_LIVE, PortfolioState, RiskEngine

TS = pd.Timestamp("2024-01-02", tz="UTC")
ZERO = CostModel(spread_bps=0, slippage_bps=0, max_volume_participation=1.0)


def pstate(eq=10_000):
    return PortfolioState(eq, eq, eq, eq, eq)


def sig(sym="AAA", d=1, entry=100.0, stop=95.0, status="PAPER", asof=TS):
    return Signal(sym, d, entry, stop, None, "s1", status, asof)


def svc(tmp_path, mode=SystemMode.PAPER, broker=None, **kw):
    mc = ModeController(mode)
    log = LogChannel()
    s = ExecutionService(broker or PaperBroker(10_000, ZERO), RiskEngine(), KillSwitch(tmp_path), mc,
                         NotificationService([log]), **kw)
    return s, log


# ------------------------------------------------------------------ paper broker
def test_paper_market_fill_next_bar_and_idempotency():
    b = PaperBroker(10_000, CostModel(spread_bps=10, slippage_bps=0, max_volume_participation=1.0))
    r = OrderRequest("c1", "AAA", Side.BUY, 10)
    st = b.submit(r)
    assert b.submit(r) is st and len(b.orders()) == 1  # duplicate order prevented
    assert st.status is OrderStatus.NEW and not b.positions()
    b.on_bar("AAA", TS, 100, 101, 99, 100.5, 1e6)
    assert st.status is OrderStatus.FILLED and st.fills[0].price == pytest.approx(100.05)
    assert b.positions() == {"AAA": 10}


def test_paper_stop_gap_limit_partial():
    b = PaperBroker(1e6, CostModel(spread_bps=0, slippage_bps=0, max_volume_participation=0.1),
                    {"AAA": InstrumentInfo("AAA", True, True, True, 1e-6)})
    b.submit(OrderRequest("buy", "AAA", Side.BUY, 100))
    b.on_bar("AAA", TS, 100, 100, 100, 100, 1e6)
    st = b.submit(OrderRequest("stop", "AAA", Side.SELL, 100, OrderType.STOP, stop_price=95))
    b.on_bar("AAA", TS, 90, 92, 89, 91, 1e6)  # gap through stop -> open
    assert st.fills[0].price == 90
    lim = b.submit(OrderRequest("lim", "AAA", Side.BUY, 500, OrderType.LIMIT, limit_price=80))
    b.on_bar("AAA", TS, 85, 86, 81, 85, 1e6)
    assert lim.status is OrderStatus.NEW
    b.on_bar("AAA", TS, 82, 83, 79, 80, 2000)  # touches 80, volume cap 200 -> partial
    assert lim.status is OrderStatus.PARTIALLY_FILLED and lim.filled_qty == 200 and lim.fills[0].price == 80


def test_paper_rejects_unshortable_and_fractional():
    b = PaperBroker(10_000, ZERO, {"AAA": InstrumentInfo("AAA", True, None, False, 1)})
    assert b.submit(OrderRequest("s", "AAA", Side.SELL, 1)).status is OrderStatus.REJECTED
    assert b.submit(OrderRequest("f", "AAA", Side.BUY, 0.5)).status is OrderStatus.REJECTED


# ------------------------------------------------------------------ execution service
def test_observation_mode_never_executes(tmp_path):
    s, _ = svc(tmp_path, SystemMode.OBSERVATION)
    assert s.handle_signal(sig(), pstate()).status == "OBSERVED"
    assert not s.broker.orders()


def test_paper_auto_submit_and_duplicate_signal(tmp_path):
    s, log = svc(tmp_path)
    o = s.handle_signal(sig(), pstate())
    assert o.status == "SUBMITTED" and o.decision.qty == pytest.approx(20)
    assert s.handle_signal(sig(), pstate()).status == "BLOCKED"  # same signal twice
    s.broker.on_bar("AAA", TS, 100, 100, 100, 100, 1e6)
    s.sync_fills()
    assert s.expected_positions == {"AAA": 20} and s.reconcile()["ok"]


def test_manual_approval_flow(tmp_path):
    s, log = svc(tmp_path, SystemMode.MANUAL_APPROVAL)
    o = s.handle_signal(sig(), pstate())
    assert o.status == "PENDING_APPROVAL" and not s.broker.orders()
    with pytest.raises(PermissionError):
        s.approve(o.signal_key, actor="ai")
    assert s.approve(o.signal_key, actor="user").status == "SUBMITTED"
    assert any(n.event is Event.TRADE_APPROVED for n in log.sent)


def test_semi_auto_threshold(tmp_path):
    s, _ = svc(tmp_path, SystemMode.SEMI_AUTOMATIC, semi_auto_max_risk_frac=0.005)
    assert s.handle_signal(sig(), pstate()).status == "PENDING_APPROVAL"  # 1% risk > 0.5%
    s2, _ = svc(tmp_path, SystemMode.SEMI_AUTOMATIC, semi_auto_max_risk_frac=0.02)
    assert s2.handle_signal(sig(sym="BBB"), pstate()).status == "SUBMITTED"


def test_kill_switch_blocks_and_cancels(tmp_path):
    s, log = svc(tmp_path)
    s.handle_signal(sig(), pstate())
    s.stop_all_trading("panic")
    assert all(o.status is OrderStatus.CANCELLED for o in s.broker.orders().values())
    assert s.handle_signal(sig(sym="BBB"), pstate()).status == "BLOCKED"
    assert any(n.event is Event.KILL_SWITCH for n in log.sent)


def test_disconnect_blocks_then_reconciles(tmp_path):
    s, log = svc(tmp_path)
    s.handle_signal(sig(), pstate())
    s.broker.on_bar("AAA", TS, 100, 100, 100, 100, 1e6)
    s.sync_fills()
    s.broker.set_connected(False)
    o = s.handle_signal(sig(sym="BBB"), pstate())
    assert o.status == "BLOCKED" and any(n.event is Event.CONNECTION_LOST for n in log.sent)
    # position changed at broker while we were offline (e.g. stop executed by broker)
    s.broker._pos["AAA"] = 0.0
    s.broker.set_connected(True)
    assert s.handle_signal(sig(sym="BBB"), pstate()).status == "BLOCKED"  # mismatch -> still blocked
    assert any(n.event is Event.RECONCILIATION_MISMATCH for n in log.sent)
    with pytest.raises(PermissionError):
        s.accept_broker_state(confirmed_by_user=False)
    s.accept_broker_state(confirmed_by_user=True)
    assert s.handle_signal(sig(sym="BBB"), pstate()).status == "SUBMITTED"


class FakeLive(PaperBroker):
    name, environment = "fake-live", "live"


def test_live_gate_blocks_by_default(tmp_path):
    ks = KillSwitch(tmp_path)
    s = ExecutionService(FakeLive(100, ZERO), RiskEngine(MICRO_LIVE), ks, ModeController(SystemMode.MANUAL_APPROVAL),
                         NotificationService(), live_gate=LiveSafetyGate(Settings(_env_file=None), ks))
    o = s.handle_signal(sig(status="ACTIVE"), pstate(100))
    assert o.status == "BLOCKED"
    assert "live trading disabled in configuration" in o.reasons
    assert "no explicit user confirmation for this live session" in o.reasons
    assert "strategy has no passed paper-trading report" in o.reasons
    gate = LiveSafetyGate(Settings(_env_file=None, env="live", live_trading_enabled=True), ks,
                          paper_report_ids={"s1": "r1"}, user_confirmed_session=True)
    s.live_gate = gate
    assert s.handle_signal(sig(status="ACTIVE", asof=TS + pd.Timedelta(days=1)), pstate(100)).status == "PENDING_APPROVAL"
    s.risk = RiskEngine()
    o = s.handle_signal(sig(status="ACTIVE", asof=TS + pd.Timedelta(days=2)), pstate(100))
    assert "micro-live requires MICRO_LIVE risk limits" in o.reasons
    assert ExecutionService(FakeLive(100, ZERO), RiskEngine(), ks, ModeController(SystemMode.MANUAL_APPROVAL),
                            NotificationService()).handle_signal(sig(), pstate(100)).reasons == ["no live safety gate configured"]


# ------------------------------------------------------------------ notifications
def test_notifications_failures_isolated():
    class Boom(Channel):
        name = "boom"

        def send(self, n):
            raise RuntimeError("down")
    log = LogChannel()
    ns = NotificationService([Boom(), WhatsAppChannel(), log], muted={Event.NEW_SIGNAL, Event.KILL_SWITCH})
    ns.notify(Event.CRITICAL_ERROR, "x")
    assert len(log.sent) == 1 and len(ns.failures) == 2
    ns.notify(Event.NEW_SIGNAL, "muted")
    ns.notify(Event.KILL_SWITCH, "critical cannot be muted")
    assert [n.event for n in log.sent] == [Event.CRITICAL_ERROR, Event.KILL_SWITCH]


def test_email_channel():
    sent = []

    class FakeSMTP:
        def __init__(self, h, p):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def login(self, u, p):
            sent.append(("login", u))

        def send_message(self, m):
            sent.append(("msg", m["Subject"]))
    EmailChannel("h", 465, "u", "p", "a@x", "b@x", smtp_cls=FakeSMTP).send(
        NotificationService().notify(Event.TRADE_CLOSED, "AAA closed"))
    assert sent == [("login", "u"), ("msg", "QSTS TRADE_CLOSED: AAA closed")]
