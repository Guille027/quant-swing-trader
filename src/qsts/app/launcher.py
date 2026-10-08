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

import html
import json
import logging
import os
import socket
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
_SERVER: dict = {"thread": None, "server": None, "errors": []}  # this process's API server
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # the local app never goes through a proxy
CLOSING_WAIT = 300.0  # seconds a new start waits for the previous QSTS to finish saving its data copy
ON_WINDOWS = sys.platform == "win32"


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
        with _OPENER.open(f"{url}/api/ping", timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001
        return False


def running_version(url: str) -> str | None:
    for path in ("/api/ping", "/api/status"):  # /api/status for versions that predate /api/ping
        try:
            with _OPENER.open(f"{url}{path}", timeout=10) as r:
                return json.loads(r.read()).get("code_version")
        except Exception:  # noqa: BLE001
            continue
    return None


def port_in_use(port: int) -> bool:
    """Is something listening on 127.0.0.1:<port> (answering or not)?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(1.0)
        return sk.connect_ex(("127.0.0.1", port)) == 0


def server_state(url: str, port: int) -> str:
    """'up' = a QSTS answers; 'stale' = something holds the port but does not answer (e.g. a previous QSTS that
    hung while closing); 'free' = nothing there."""
    if is_running(url, 2.0):
        return "up"
    if not port_in_use(port):
        return "free"
    for _ in range(3):  # a busy app can be slow to answer: give it a few seconds
        if is_running(url, 3.0):
            return "up"
    return "stale" if port_in_use(port) else "free"


def _server_alive() -> bool:
    t = _SERVER["thread"]
    return t is None or t.is_alive()


def wait_ready(url: str, timeout: float = 180.0, alive=_server_alive) -> bool:
    """Wait for the app to answer; give up at once if this process's server stopped (e.g. the port was taken)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(url, 2.0):
            return True
        if alive is not None and not alive():
            return False
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


def _state_file(root: Path) -> Path:
    return root / "var" / "launcher.state"


def set_state(root: Path, state: str | None) -> None:
    """What the launcher holding the lock is doing ('opening', 'open', 'closing'); None when it is done."""
    try:
        if state is None:
            _state_file(root).unlink(missing_ok=True)
        else:
            _state_file(root).write_text(state, encoding="utf-8")
    except OSError:
        pass


def get_state(root: Path) -> str | None:
    try:
        f = _state_file(root)
        if time.time() - f.stat().st_mtime > 3600:  # left over by a crash
            return None
        return f.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


SPLASH = """<!doctype html><html><head><meta charset="utf-8"><style>
body{background:#0f1218;color:#e6e9ef;font-family:Segoe UI,system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0}div{text-align:center}h1{font-weight:600;margin:0 0 12px}
p{color:#8b93a1;max-width:520px;line-height:1.5}.dot{animation:b 1.2s infinite}@keyframes b{50%%{opacity:.2}}
</style></head><body><div><h1>Abriendo QSTS<span class="dot">…</span></h1><p>%s</p></div></body></html>"""
ERROR_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{background:#0f1218;color:#e6e9ef;font-family:Segoe UI,system-ui,sans-serif;margin:0;padding:28px}
h1{font-weight:600;margin:0 0 12px;font-size:22px}p{color:#c9cdd4;max-width:900px;line-height:1.5}
pre{background:#0b0e12;border:1px solid #262c36;border-radius:6px;padding:10px;white-space:pre-wrap;font-size:12px;
color:#8b93a1;max-height:55vh;overflow:auto}</style></head><body><h1>QSTS no ha podido abrirse</h1>%s</body></html>"""
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


def _image_name(pid: int) -> str:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out.split(",")[0].strip('"') if out.startswith('"') else ""
    except Exception:  # noqa: BLE001
        return ""


def _end_python(pid: int | None) -> bool:
    """End process `pid` if (and only if) it is a python process other than this one (an old QSTS)."""
    if not pid or pid == os.getpid() or not ON_WINDOWS:
        return False
    if "python" not in _image_name(pid).lower():  # never touch anything that is not a python process
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
    wait_for_exit(pid, 15)
    return True


def stop_running(url: str, port: int) -> bool:
    """Close the running (older) QSTS: its server first, then its process, so its window and any research it was
    running close too (they would keep writing to the same data with old code)."""
    pid = _pid_listening(port) if ON_WINDOWS else None
    try:
        req = urllib.request.Request(f"{url}/api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        _OPENER.open(req, timeout=3).close()
    except Exception:  # noqa: BLE001 - too old to know how: end its process
        _end_python(pid)
    for _ in range(60):
        if not is_running(url, 0.5):
            break
        time.sleep(0.25)
    else:
        return False
    _end_python(pid)
    return True


def clear_stale(port: int) -> str | None:
    """Something holds the port without answering. If it is a python process (a previous QSTS that hung while
    closing) it is ended. Returns None when the port is free again, otherwise what to tell the user."""
    pid = _pid_listening(port) if ON_WINDOWS else None
    name = _image_name(pid) if pid else ""
    if pid and "python" in name.lower():
        print(f"ending a previous QSTS that holds port {port} without answering (PID {pid})")
        _end_python(pid)
    for _ in range(40):
        if not port_in_use(port):
            return None
        time.sleep(0.25)
    who = f"el programa {name} (PID {pid})" if pid and name else "otro programa"
    return (f"El puerto {port}, que usa QSTS, está ocupado por {who} y no responde. Reinicia el ordenador y vuelve "
            "a abrir QSTS. Si se repite, cierra ese programa o pásale esta pantalla a Claude.")


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
    errors: list[str] = []

    class Capture(logging.Handler):  # uvicorn reports e.g. "port already in use" through its logger, then exits
        def emit(self, record):
            errors.append(record.getMessage()[:500])
    logging.getLogger("uvicorn.error").addHandler(Capture(logging.ERROR))

    def serve():
        try:
            server.run()
        except BaseException as e:  # noqa: BLE001 - SystemExit when it cannot start
            errors.append(f"el servidor se ha detenido: {e!r}")
            print(f"server stopped: {e!r}")
    t = threading.Thread(target=serve, daemon=True, name="api")
    _SERVER.update(thread=t, server=server, errors=errors)
    t.start()
    return ctx


def stop_server(timeout: float = 5.0) -> None:
    """Close this process's server (and its port) before exiting."""
    server, t = _SERVER["server"], _SERVER["thread"]
    if server is not None:
        server.should_exit = True
    if t is not None:
        t.join(timeout)


def log_tail(root: Path, lines: int = 25) -> str:
    """The last lines this start wrote to var/desktop.log."""
    try:
        with open(root / "var" / "desktop.log", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    text = text[text.rfind("--- "):] if "--- " in text else text
    return "\n".join(text.splitlines()[-lines:])


def failure_page(root: Path, reason: str) -> str:
    errs = _SERVER["errors"][-5:]
    body = f"<p>{html.escape(reason)}</p>"
    if errs:
        body += "<p>Motivo:</p><pre>" + html.escape("\n".join(errs)) + "</pre>"
    body += ("<p>Cierra esta ventana y vuelve a abrir QSTS. Si se repite, reinicia el ordenador; si aun así no abre, "
             "haz una captura de esta pantalla y pásasela a Claude.</p>")
    tail = log_tail(root)
    if tail:
        body += "<p>Últimas líneas del registro (var\\desktop.log):</p><pre>" + html.escape(tail) + "</pre>"
    return ERROR_PAGE % body


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
    set_state(project_root(), "closing")
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
    if lock is None and get_state(root) == "closing":  # the previous QSTS is still saving its data copy
        print("the previous QSTS is closing (saving its data copy): waiting for it")
        deadline = time.monotonic() + CLOSING_WAIT
        while lock is None and time.monotonic() < deadline:
            time.sleep(0.5)
            lock = single_instance(root)
    if lock is None:
        print("another launcher is already opening/running QSTS: nothing to do")
        return
    set_state(root, "opening")
    try:
        _run(url, port)
    finally:
        set_state(root, None)
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
    root = project_root()
    state = server_state(url, port)
    if state == "stale":
        problem = clear_stale(port)
        if problem:
            print(f"port problem: {problem}")
            message(problem)
            return
    if state == "up":
        from qsts.core.version import code_version
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
                    t0 = time.monotonic()
                    if not is_running(url):
                        box["ctx"] = _start_or_explain(port)
                    if wait_ready(url):
                        print(f"ready after {time.monotonic() - t0:.1f}s")
                        set_state(root, "open")
                        win.load_url(url)
                    else:
                        print("the app did not answer: " + "; ".join(_SERVER["errors"][-3:]))
                        win.load_html(failure_page(root, "La parte interna de QSTS no ha respondido."))
                except Exception as e:  # noqa: BLE001
                    print(f"boot failed: {e!r}")
                    win.load_html(failure_page(root, str(e)))
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
            message("QSTS no ha podido abrirse.\n\n" + "\n".join(_SERVER["errors"][-3:]) +
                    "\n\nÚltimas líneas del registro (var\\desktop.log):\n" + log_tail(root, 12))
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
    try:
        main()
    finally:
        # never linger after the window is closed: a process stuck while exiting would keep the port and the
        # data busy, and the next start could not open
        stop_server()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
