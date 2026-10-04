"""Double-click launcher used by the desktop shortcut (runs under pythonw: no console window).

- Always runs from the project folder, so `.env` and `var/` are found wherever it is started from.
- If the SAME version is already open, it only opens another window onto it (no second copy of the server).
  If an OLDER version is still running (e.g. after `git pull`), it is closed first so the new code is used.
- There is no console under pythonw: output goes to var/desktop.log.
- If the native window cannot be created, the app opens in the browser and a small dialog keeps it alive
  ("press OK to close the app").
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8765
TITLE = "QSTS — Investigación de estrategias"


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]  # src/qsts/app/launcher.py -> project folder


def _redirect_output(root: Path) -> None:
    if sys.stdout is None or sys.stderr is None:  # pythonw
        (root / "var").mkdir(exist_ok=True)
        log = open(root / "var" / "desktop.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def is_running(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/status", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def running_version(url: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{url}/api/status", timeout=2) as r:
            return json.loads(r.read()).get("code_version")
    except Exception:  # noqa: BLE001
        return None


def _pid_listening(port: int) -> int | None:
    """Windows: PID of the process listening on 127.0.0.1:<port> (netstat), or None."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() in ("LISTENING", "ESCUCHANDO"):
            return int(parts[4])
    return None


def stop_running(url: str, port: int) -> bool:
    """Ask the running app to stop; if it is too old to know how, end the python process holding the port."""
    try:
        req = urllib.request.Request(f"{url}/api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).close()
    except Exception:  # noqa: BLE001
        if sys.platform == "win32":
            pid = _pid_listening(port)
            if pid:
                img = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                     capture_output=True, text=True, timeout=10).stdout.lower()
                if "python" in img:  # never touch anything that is not a python process
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
    for _ in range(60):
        if not is_running(url, 0.5):
            return True
        time.sleep(0.25)
    return False


def message(text: str, title: str = "QSTS") -> None:
    """Blocking information dialog on Windows; plain print elsewhere."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)  # MB_ICONINFORMATION
    else:
        print(f"{title}: {text}")


def start_server(port: int) -> None:
    import uvicorn
    from qsts.api.server import create_app
    from qsts.app.context import build_context
    ctx = build_context()
    server = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1", port=port, log_level="warning"))
    ctx.extra["shutdown"] = lambda: setattr(server, "should_exit", True)
    threading.Thread(target=server.run, daemon=True, name="api").start()


def main(port: int = PORT) -> None:
    root = project_root()
    os.chdir(root)
    _redirect_output(root)
    url = f"http://127.0.0.1:{port}"
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} launcher start ({url})")
    if is_running(url):
        from qsts.research.experiments import code_version
        mine, theirs = code_version(), running_version(url)
        if theirs != mine:
            print(f"replacing running version {theirs} with {mine}")
            message("Hay una versión anterior de QSTS abierta.\n\nSe cerrará para abrir la versión nueva "
                    "(si estaba investigando, esa investigación se detiene; lo ya probado queda guardado).")
            if not stop_running(url, port):
                message("No se pudo cerrar la versión anterior. Reinicia el ordenador y vuelve a abrir QSTS.")
                return
    if not is_running(url):
        try:
            start_server(port)
        except Exception as e:  # noqa: BLE001
            message(f"No se pudo arrancar QSTS:\n\n{e!r}\n\nDetalles en var\\desktop.log")
            raise
        for _ in range(120):
            if is_running(url, 0.5):
                break
            time.sleep(0.25)
        else:
            message("QSTS no ha arrancado a tiempo. Detalles en var\\desktop.log")
            return
    try:
        import webview
        webview.create_window(TITLE, url, width=1400, height=900, min_size=(900, 600), maximized=True,
                              confirm_close=True, text_select=True)
        webview.start(localization={
            "global.quitConfirmation": "¿Cerrar QSTS?\n\nSi hay una investigación o una descarga en marcha, se detendrá.",
            "global.ok": "Aceptar", "global.quit": "Salir", "global.cancel": "Cancelar"})
    except Exception as e:  # noqa: BLE001 - no native window (e.g. WebView2 missing): use the browser
        print(f"native window unavailable: {e!r}")
        webbrowser.open(url)
        message("QSTS está abierta en tu navegador.\n\nDeja este aviso abierto mientras la uses: al pulsar "
                "Aceptar se CIERRA la app (y se detiene cualquier investigación en marcha).")
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} closed")


if __name__ == "__main__":
    main()
