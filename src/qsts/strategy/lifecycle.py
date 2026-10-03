"""Strategy lifecycle state machine.

RESEARCH -> BACKTESTED -> VALIDATING -> CANDIDATE -> PAPER -> APPROVED -> ACTIVE
ACTIVE -> DEGRADED -> UNDER_REVIEW -> (DISABLED | PAPER | ACTIVE)
Any state -> REJECTED (research failure) or DISABLED. Nothing is ever deleted: every transition is
appended to strategy_status_history with reason and actor.

Promotion gates require evidence objects; AI- or evolution-generated strategies can never skip
stages, and APPROVED / ACTIVE always require a human actor.
"""
from __future__ import annotations

from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from qsts.db import models as m
from qsts.strategy.definition import StrategyDefinition


class Status(str, Enum):
    RESEARCH = "RESEARCH"
    BACKTESTED = "BACKTESTED"
    VALIDATING = "VALIDATING"
    CANDIDATE = "CANDIDATE"
    PAPER = "PAPER"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    UNDER_REVIEW = "UNDER_REVIEW"
    DISABLED = "DISABLED"
    REJECTED = "REJECTED"


S = Status
ALLOWED: dict[Status, set[Status]] = {
    S.RESEARCH: {S.BACKTESTED, S.REJECTED, S.DISABLED},
    S.BACKTESTED: {S.VALIDATING, S.REJECTED, S.DISABLED},
    S.VALIDATING: {S.CANDIDATE, S.REJECTED, S.DISABLED},
    S.CANDIDATE: {S.PAPER, S.REJECTED, S.DISABLED},
    S.PAPER: {S.APPROVED, S.UNDER_REVIEW, S.REJECTED, S.DISABLED},
    S.APPROVED: {S.ACTIVE, S.UNDER_REVIEW, S.DISABLED},
    S.ACTIVE: {S.DEGRADED, S.UNDER_REVIEW, S.DISABLED},
    S.DEGRADED: {S.UNDER_REVIEW, S.DISABLED, S.ACTIVE},
    S.UNDER_REVIEW: {S.DISABLED, S.PAPER, S.ACTIVE, S.REJECTED},
    S.DISABLED: {S.UNDER_REVIEW},
    S.REJECTED: {S.RESEARCH},  # only by creating a re-research record; history kept
}
HUMAN_ONLY = {S.APPROVED, S.ACTIVE}
# evidence keys required to ENTER a state
REQUIRED_EVIDENCE: dict[Status, set[str]] = {
    S.BACKTESTED: {"backtest_experiment_id"},
    S.CANDIDATE: {"walk_forward_experiment_id", "robustness_passed", "oos_experiment_id", "monte_carlo_experiment_id"},
    S.APPROVED: {"paper_trading_report_id"},
}


class LifecycleError(RuntimeError):
    pass


class StrategyRegistry:
    def __init__(self, sf: sessionmaker[Session]):
        self.sf = sf

    def register(self, strategy_id: str, sd: StrategyDefinition, origin: str = "human",
                 parent_version_id: str | None = None) -> str:
        """Create strategy (if new) and append a version. Returns the version id."""
        vid = sd.version_id
        with self.sf() as s, s.begin():
            st = s.get(m.Strategy, strategy_id)
            if st is None:
                st = m.Strategy(id=strategy_id, name=sd.name, family=sd.family, status=S.RESEARCH.value, origin=origin)
                s.add(st)
                s.add(m.StrategyStatusHistory(strategy_id=strategy_id, from_status=None, to_status=S.RESEARCH.value,
                                              reason="created", actor=origin))
                s.flush()
            if s.get(m.StrategyVersion, vid) is None:
                n = len(s.scalars(select(m.StrategyVersion.id).where(m.StrategyVersion.strategy_id == strategy_id)).all())
                s.add(m.StrategyVersion(id=vid, strategy_id=strategy_id, version=n + 1, definition=sd.to_dict(),
                                        parent_version_id=parent_version_id))
        return vid

    def status(self, strategy_id: str) -> Status:
        with self.sf() as s:
            return Status(s.get(m.Strategy, strategy_id).status)

    def transition(self, strategy_id: str, target: Status, *, reason: str, actor: str,
                   evidence: dict | None = None) -> None:
        evidence = evidence or {}
        with self.sf() as s, s.begin():
            st = s.get(m.Strategy, strategy_id)
            if st is None:
                raise LifecycleError(f"unknown strategy {strategy_id}")
            cur = Status(st.status)
            if target not in ALLOWED[cur]:
                raise LifecycleError(f"{cur.value} -> {target.value} not allowed")
            if target in HUMAN_ONLY and actor in ("ai", "evolution", "system"):
                raise LifecycleError(f"{target.value} requires a human actor")
            missing = REQUIRED_EVIDENCE.get(target, set()) - {k for k, v in evidence.items() if v}
            if missing:
                raise LifecycleError(f"missing evidence for {target.value}: {sorted(missing)}")
            st.status = target.value
            s.add(m.StrategyStatusHistory(strategy_id=strategy_id, from_status=cur.value, to_status=target.value,
                                          reason=f"{reason} | evidence={evidence}" if evidence else reason, actor=actor))

    def history(self, strategy_id: str) -> list[tuple[str | None, str, str]]:
        with self.sf() as s:
            rows = s.scalars(select(m.StrategyStatusHistory).where(m.StrategyStatusHistory.strategy_id == strategy_id)
                             .order_by(m.StrategyStatusHistory.id)).all()
            return [(r.from_status, r.to_status, r.actor) for r in rows]
