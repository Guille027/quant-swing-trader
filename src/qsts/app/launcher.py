"""Double-click launcher used by the desktop shortcut (runs under pythonw: no console window).

- Always runs from the project folder, so `.env` and `var/` are found wherever it is started from.
- If the app is already open, it only opens another window onto it (no second copy of the server).
- There is no console under pythonw: output goes to var/desktop.log.
- If the native window cannot be created, the app opens in the browser and a small dialog keeps it alive
  ("press OK to close the app").
"""
from __future__ import annotations

import os
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
    server = uvicorn.Server(uvicorn.Config(create_app(build_context()), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="api").start()


def main(port: int = PORT) -> None:
    root = project_root()
    os.chdir(root)
    _redirect_output(root)
    url = f"http://127.0.0.1:{port}"
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} launcher start ({url})")
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
