"""The nightly run (Windows Task Scheduler, 'Programar QSTS.bat'): wakes the PC, downloads the day's prices, lets the
paper trader handle the last close's signals (orders queued for the next open), and exits. If the QSTS window is
already open, it does nothing: the open app does the same work (two processes must never send orders together)."""
from __future__ import annotations

import time
import urllib.request
from datetime import datetime
from typing import Callable

DONE = ("señales del cierre", "al día", "sin bots", "desactivado", "Alpaca no está conectado")


def app_is_open(port: int = 8765) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def run_once(ctx, max_minutes: float = 90, sleep: Callable[[float], None] = time.sleep,
             log: Callable[[str], None] = print) -> str:
    from qsts.api.server import create_app
    ctx.extra.setdefault("trader_autostart", False)
    create_app(ctx)  # builds the same services the app uses (lab, trader, downloads)
    trader = ctx.extra["make_trader"]()
    prices = ctx.extra["auto_update_tick"]
    data_busy = ctx.extra["data_busy"]
    end = time.monotonic() + max_minutes * 60
    state = ""
    while time.monotonic() < end:
        try:
            prices()
        except Exception as e:  # noqa: BLE001 - a failed download is retried, never fatal
            log(f"precios: {e!r}")
        state = trader.tick()
        log(state)
        if state.startswith(DONE) and not data_busy():
            return state
        sleep(30)
    trader.notify(f"⚠️ La revisión nocturna de QSTS no terminó a tiempo. Último estado: {state}. "
                  "Abre la app para que se ponga al día antes de las 15:30.")
    return state


def main(ctx, port: int = 8765) -> None:
    from pathlib import Path
    logf = Path(ctx.settings.state_dir) / "nightly.log"
    logf.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        with open(logf, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    if app_is_open(port):
        log("la ventana de QSTS está abierta: ella se encarga")
        return
    log("revisión nocturna: empieza")
    state = run_once(ctx, log=log)
    log("revisión nocturna: termina — " + state)
    if state.startswith(DONE):  # every night a short message, so silence means it did not run
        ctx.extra["make_trader"]().notify(f"🌙 Revisión nocturna de QSTS hecha: {state}.")
