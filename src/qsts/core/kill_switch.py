"""Emergency kill switch: STOP ALL TRADING.

Deliberately implemented as a plain file flag with no dependency on the database,
broker, network or any other component, so it keeps working when they fail.
Fail-safe: if the flag state cannot be determined, trading is treated as STOPPED.
Engaging the switch never closes positions; closing requires a separate explicit user action.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class KillSwitch:
    FILENAME = "KILL_SWITCH"

    def __init__(self, state_dir: Path | str):
        self.path = Path(state_dir) / self.FILENAME

    def engage(self, reason: str, actor: str = "user") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"reason": reason, "actor": actor,
                   "engaged_at": datetime.now(timezone.utc).isoformat()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self.path)  # atomic

    def release(self, *, confirmed_by_user: bool) -> None:
        if not confirmed_by_user:
            raise PermissionError("Releasing the kill switch requires explicit user confirmation")
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def is_engaged(self) -> bool:
        try:
            return self.path.exists()
        except OSError:
            return True  # cannot determine -> fail safe

    def trading_allowed(self) -> bool:
        return not self.is_engaged()

    def info(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"reason": "unreadable kill switch file (treated as engaged)"}
