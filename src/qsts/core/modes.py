"""System operating modes and their strict progression.

OBSERVATION -> BACKTEST -> PAPER -> MANUAL_APPROVAL -> SEMI_AUTOMATIC -> FULL_AUTOMATIC

A mode may only advance one step at a time, and only through an explicit call.
Nothing in the system advances modes automatically. Downgrades are always allowed.
"""
from __future__ import annotations

from enum import IntEnum


class SystemMode(IntEnum):
    OBSERVATION = 0
    BACKTEST = 1
    PAPER = 2
    MANUAL_APPROVAL = 3
    SEMI_AUTOMATIC = 4
    FULL_AUTOMATIC = 5

    @property
    def can_execute_orders(self) -> bool:
        return self >= SystemMode.PAPER

    @property
    def uses_real_money(self) -> bool:
        # Paper is simulated. Real money is only possible from MANUAL_APPROVAL upward,
        # and additionally requires the live safety system (phase 20).
        return self >= SystemMode.MANUAL_APPROVAL


class ModeTransitionError(RuntimeError):
    pass


class ModeController:
    def __init__(self, initial: SystemMode = SystemMode.OBSERVATION):
        self._mode = initial
        self.history: list[tuple[SystemMode, SystemMode, str]] = []

    @property
    def mode(self) -> SystemMode:
        return self._mode

    def transition(self, target: SystemMode, *, reason: str, confirmed_by_user: bool = False) -> None:
        if target == self._mode:
            return
        if target > self._mode:
            if target - self._mode != 1:
                raise ModeTransitionError(f"Cannot skip stages: {self._mode.name} -> {target.name}")
            if target.uses_real_money and not confirmed_by_user:
                raise ModeTransitionError(f"{target.name} requires explicit user confirmation")
        self.history.append((self._mode, target, reason))
        self._mode = target
