"""Double-click launcher used by the desktop shortcut (runs under pythonw: no console window).

- Always runs from the project folder, so `.env` and `var/` are found wherever it is started from.
- If the SAME version is already open, it only opens another window onto it (no second copy of the server).
  If an OLDER version is still running (e.g. after `git pull`), it is closed first so the new code is used.
- There is no console under pythonw: output goes to var/desktop.log.
- If the native window cannot be created, the app opens in the browser and a small dialog keeps it alive
  ("press OK to close the app").
- Several computers (qsts.app.sync): when the window closes, the data copy is saved to the shared folder; after
  loading a copy from another computer the app restarts by itself to use it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8765
TITLE = "QSTS — Investigación de estrategias"
_RESTART = threading.Event()


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]  # src/qsts/app/launcher.py -> project folder


def _redirect_output(root: Path) -> None:
    if sys.stdout is None or sys.stderr is None:  # pythonw
        (root / "var").mkdir(exist_ok=True)
        log = open(root / "var" / "desktop.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def is_running(url: str, timeout: float = 1.0) -> bool:
    """Is a QSTS server answering on `url`? Uses the instant /api/ping (any HTTP answer, even 404 from a version
    without it, means a server is up)."""
    try:
        with urllib.request.urlopen(f"{url}/api/ping", timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001
        return False


def running_version(url: str) -> str | None:
    for path in ("/api/ping", "/api/status"):  # /api/status for versions that predate /api/ping
        try:
            with urllib.request.urlopen(f"{url}{path}", timeout=10) as r:
                return json.loads(r.read()).get("code_version")
        except Exception:  # noqa: BLE001
            continue
    return None


def wait_ready(url: str, timeout: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(url, 2.0):
            return True
        time.sleep(0.3)
    return False


def single_instance(root: Path):
    """Only one launcher at a time (a second double-click while QSTS is opening must not open it twice).
    Returns the open lock file (keep it until exit) or None if another launcher holds it."""
    (root / "var").mkdir(exist_ok=True)
    f = open(root / "var" / "launcher.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


SPLASH = """<!doctype html><html><head><meta charset="utf-8"><style>
body{background:#0f1218;color:#e6e9ef;font-family:Segoe UI,system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0}div{text-align:center}h1{font-weight:600;margin:0 0 12px}
p{color:#8b93a1;max-width:520px;line-height:1.5}.dot{animation:b 1.2s infinite}@keyframes b{50%%{opacity:.2}}
</style></head><body><div><h1>Abriendo QSTS<span class="dot">…</span></h1><p>%s</p></div></body></html>"""
WAIT_TEXT = ("Cargando tus datos. La primera vez después de encender el ordenador (o tras cargar una copia de otro "
             "ordenador) puede tardar hasta un minuto. No hace falta volver a pulsar el icono.")


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


def _close_windows() -> None:
    try:
        import webview
        for w in list(webview.windows):
            w.destroy()
    except Exception as e:  # noqa: BLE001
        print(f"could not close the window: {e!r}")


def _restart_soon() -> None:
    _RESTART.set()
    threading.Timer(1.5, _close_windows).start()  # let the screen show its message first


def start_server(port: int):
    import uvicorn
    from qsts.api.server import create_app
    from qsts.app.context import build_context
    ctx = build_context()
    if ctx.extra.get("sync_loaded"):
        print(f"loaded the data copy from {ctx.extra['sync_loaded'].get('machine')}")
    server = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1", port=port, log_level="warning"))
    ctx.extra["shutdown"] = lambda: setattr(server, "should_exit", True)
    ctx.extra["restart_app"] = _restart_soon
    threading.Thread(target=server.run, daemon=True, name="api").start()
    return ctx


def _release_data(ctx) -> None:
    """Close this process's handles on the database so the next start can swap in the loaded copy."""
    rep = ctx.extra.get("daily_reporter")
    if rep is not None:
        rep.stop()
    try:
        engine = ctx.sf.kw.get("bind")
        if engine is not None:
            engine.dispose()
    except Exception as e:  # noqa: BLE001
        print(f"could not release the database: {e!r}")


def wait_for_exit(pid: int, timeout: float = 60.0) -> None:
    """Wait until process `pid` (the previous QSTS) has fully exited and released its files."""
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:
            k32.WaitForSingleObject(handle, int(timeout * 1000))
            k32.CloseHandle(handle)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def after_close(ctx) -> None:
    """Window closed: restart if a copy from another computer was loaded, otherwise save this computer's copy."""
    if ctx is None:
        return  # another process owns the server and the data
    if _RESTART.is_set():
        print("restarting to use the loaded data copy")
        _release_data(ctx)
        subprocess.Popen([sys.executable, "-m", "qsts.app.launcher", "--wait-pid", str(os.getpid())],
                         cwd=str(project_root()))
        return
    try:
        from qsts.app.sync import auto_save_on_close
        print("sync: " + auto_save_on_close(ctx.settings.database_url, ctx.settings.state_dir,
                                            ctx.extra.get("code_version")))
    except Exception as e:  # noqa: BLE001 - never fail on exit; the user can save from the app
        print(f"sync: copy NOT saved: {e!r}")


def main(port: int = PORT, argv: list[str] | None = None) -> None:
    root = project_root()
    os.chdir(root)
    _redirect_output(root)
    url = f"http://127.0.0.1:{port}"
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} launcher start ({url})")
    args = argv if argv is not None else sys.argv[1:]
    if "--wait-pid" in args:  # restart after loading data: the previous QSTS must have exited completely
        try:
            wait_for_exit(int(args[args.index("--wait-pid") + 1]))
        except (IndexError, ValueError):
            pass
    if "--wait-pid" in args or "--wait-free" in args:
        for _ in range(80):
            if not is_running(url, 0.5):
                break
            time.sleep(0.25)
    lock = single_instance(root)
    if lock is None:
        print("another launcher is already opening/running QSTS: nothing to do")
        return
    try:
        _run(url, port)
    finally:
        lock.close()
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} closed")


def _start_or_explain(port: int):
    try:
        return start_server(port)
    except Exception as e:  # noqa: BLE001
        from qsts.app.sync import SyncError
        detail = str(e) if isinstance(e, SyncError) else repr(e)
        print(f"start failed: {e!r}")
        raise RuntimeError(f"No se pudo arrancar QSTS:\n\n{detail}\n\nDetalles en var\\desktop.log") from e


def _run(url: str, port: int) -> None:
    ctx = None
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        webview, gui_error = None, e
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
    box = {"ctx": None}
    if webview is not None:  # the window appears at once with "Abriendo QSTS…"; the app loads behind it
        try:
            win = webview.create_window(TITLE, html=SPLASH % WAIT_TEXT, width=1400, height=900, min_size=(900, 600),
                                        maximized=True, confirm_close=True, text_select=True)

            def boot():
                try:
                    if not is_running(url):
                        box["ctx"] = _start_or_explain(port)
                    if wait_ready(url):
                        win.load_url(url)
                    else:
                        win.load_html(SPLASH % "QSTS no ha arrancado a tiempo. Cierra esta ventana y vuelve a abrirla; "
                                               "si se repite, mira var\\desktop.log.")
                except Exception as e:  # noqa: BLE001
                    print(f"boot failed: {e!r}")
                    win.load_html(SPLASH % str(e).replace("\n", "<br>"))
            webview.start(boot, localization={
                "global.quitConfirmation": "¿Cerrar QSTS?\n\nSi hay una investigación o una descarga en marcha, se detendrá.",
                "global.ok": "Aceptar", "global.quit": "Salir", "global.cancel": "Cancelar"})
            after_close(box["ctx"])
            return
        except Exception as e:  # noqa: BLE001 - the native window could not be created (e.g. WebView2 missing)
            gui_error = e
            ctx = box["ctx"]
    # ---- fallback: the browser, kept alive by a small dialog
    if ctx is None and not is_running(url):
        try:
            ctx = _start_or_explain(port)
        except RuntimeError as e:
            message(str(e))
            raise
        if not wait_ready(url):
            message("QSTS no ha arrancado a tiempo. Detalles en var\\desktop.log")
            return
    print(f"native window unavailable: {gui_error!r}")
    why = ("Falta el componente de la ventana propia (pywebview): cierra QSTS y haz doble clic en "
           "'Instalar QSTS.bat' en la carpeta del proyecto." if isinstance(gui_error, ImportError) else
           "Windows no pudo crear la ventana propia: instala 'Microsoft Edge WebView2 Runtime' (gratis, "
           "de la web de Microsoft) y vuelve a abrir QSTS.")
    webbrowser.open(url)
    message("QSTS está abierta en tu navegador.\n\n" + why + "\n\nDeja este aviso abierto mientras la uses: "
            "al pulsar Aceptar se CIERRA la app (y se detiene cualquier investigación en marcha).")
    after_close(ctx)


if __name__ == "__main__":
    main()
