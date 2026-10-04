"""Keep Windows from sleeping while a long job (research loop, download) runs. The request belongs to the calling
thread and is released when it calls keep_awake(False) or exits. The screen may still turn off. No-op elsewhere."""
from __future__ import annotations

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on: bool) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:  # noqa: BLE001 - never let power management break a job
        return False
