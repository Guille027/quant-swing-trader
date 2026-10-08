import os
import socket
import sys

from qsts.app import launcher
from qsts.app.shortcut import shortcut_script
from qsts.core.power import keep_awake


def test_project_root_and_icon():
    root = launcher.project_root()
    assert (root / "pyproject.toml").exists() and (root / "src" / "qsts" / "ui" / "static" / "qsts.ico").exists()


def test_shortcut_script_quotes_paths(tmp_path):
    odd = tmp_path / "Mis cosas" / "O'Brien"
    s = shortcut_script(odd / "QSTS.lnk", odd / "pythonw.exe", "-m qsts.app.launcher", odd, odd / "qsts.ico", "d")
    assert "O''Brien" in s and "'-m qsts.app.launcher'" in s and ",0'" in s and s.endswith("$s.Save()")


def test_is_running_false_on_free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    assert launcher.is_running(f"http://127.0.0.1:{port}", 0.2) is False


def test_launcher_falls_back_to_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "is_running", lambda url, timeout=1.0: True)  # app already open
    from qsts.core.version import code_version
    monkeypatch.setattr(launcher, "running_version", lambda url: code_version())  # ... and it is this version
    monkeypatch.setattr(launcher, "start_server", lambda port: calls.append("server"))
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: calls.append(("browser", url)))
    monkeypatch.setattr(launcher, "message", lambda text, title="QSTS": calls.append("dialog"))
    monkeypatch.setitem(sys.modules, "webview", None)  # no native window available
    cwd = os.getcwd()
    try:
        launcher.main(port=8799)
    finally:
        os.chdir(cwd)
    assert calls == [("browser", "http://127.0.0.1:8799"), "dialog"]  # no second server, browser + keep-alive dialog


def test_keep_awake_is_noop_off_windows():
    assert keep_awake(True) is (sys.platform == "win32" and keep_awake(True))
    keep_awake(False)


def test_launcher_replaces_an_older_running_version(monkeypatch):
    calls, state = [], {"up": True}
    monkeypatch.setattr(launcher, "is_running", lambda url, timeout=1.0: state["up"])
    monkeypatch.setattr(launcher, "running_version", lambda url: "old-version")
    def stop(url, port):
        calls.append("stop-old")
        state["up"] = False
        return True
    def start(port):
        calls.append("start-new")
        state["up"] = True
    monkeypatch.setattr(launcher, "stop_running", stop)
    monkeypatch.setattr(launcher, "start_server", start)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: calls.append("browser"))
    monkeypatch.setattr(launcher, "message", lambda text, title="QSTS": calls.append("dialog"))
    monkeypatch.setitem(sys.modules, "webview", None)
    cwd = os.getcwd()
    try:
        launcher.main(port=8798)
    finally:
        os.chdir(cwd)
    assert calls == ["dialog", "stop-old", "start-new", "browser", "dialog"]


def test_only_one_launcher_at_a_time(tmp_path):
    first = launcher.single_instance(tmp_path)
    assert first is not None and launcher.single_instance(tmp_path) is None  # a second double-click does nothing
    first.close()
    again = launcher.single_instance(tmp_path)
    assert again is not None
    again.close()


def test_is_running_uses_an_instant_ping_and_accepts_old_versions():
    import http.server
    import threading
    class Old(http.server.BaseHTTPRequestHandler):  # a version without /api/ping answers 404: it IS running
        def do_GET(self):
            self.send_response(404)
            self.end_headers()
        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Old)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert launcher.is_running(f"http://127.0.0.1:{srv.server_port}", 1.0) is True
    finally:
        srv.shutdown()


def test_window_opens_at_once_and_loads_the_app_when_ready(monkeypatch):
    from types import SimpleNamespace
    calls = []
    class Win:
        def load_url(self, url):
            calls.append(("load_url", url))
        def load_html(self, html):
            calls.append(("load_html", html[:40]))
    fake = SimpleNamespace(windows=[])
    def create_window(title, url=None, html=None, **kw):
        calls.append(("window", "splash" if html and "Abriendo QSTS" in html else url))
        return Win()
    def start(func=None, *a, **kw):
        func()  # pywebview runs it in a thread once the window exists
    fake.create_window, fake.start = create_window, start
    monkeypatch.setitem(sys.modules, "webview", fake)
    state = {"up": False}
    monkeypatch.setattr(launcher, "is_running", lambda url, timeout=1.0: state["up"])
    def server(port):
        calls.append("server")
        state["up"] = True
        return "ctx"
    monkeypatch.setattr(launcher, "start_server", server)
    monkeypatch.setattr(launcher, "after_close", lambda ctx: calls.append(("after_close", ctx)))
    cwd = os.getcwd()
    try:
        launcher.main(port=8797, argv=[])
    finally:
        os.chdir(cwd)
    assert calls == [("window", "splash"), "server", ("load_url", "http://127.0.0.1:8797"), ("after_close", "ctx")]


def test_a_stuck_previous_qsts_on_the_port_is_ended(monkeypatch):
    """A previous QSTS that hung while closing keeps the port without answering: it is ended, then QSTS starts."""
    held = {"busy": True}
    ended, real_end = [], launcher._end_python
    monkeypatch.setattr(launcher, "ON_WINDOWS", True)
    monkeypatch.setattr(launcher, "is_running", lambda url, timeout=1.0: False)
    monkeypatch.setattr(launcher, "port_in_use", lambda port: held["busy"])
    monkeypatch.setattr(launcher, "_pid_listening", lambda port: 4242)
    monkeypatch.setattr(launcher, "_image_name", lambda pid: "pythonw.exe")
    def end(pid):
        ended.append(pid)
        held["busy"] = False
        return True
    monkeypatch.setattr(launcher, "_end_python", end)
    assert launcher.server_state("http://127.0.0.1:1", 1) == "stale"
    assert launcher.clear_stale(1) is None and ended == [4242]
    # something that is not python is never touched: the user is told what holds the port
    held["busy"] = True
    monkeypatch.setattr(launcher, "_image_name", lambda pid: "OtroPrograma.exe")
    monkeypatch.setattr(launcher, "_end_python", real_end)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("killed")))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    msg = launcher.clear_stale(1)
    assert msg and "OtroPrograma.exe" in msg and "4242" in msg


def test_a_server_that_cannot_start_is_reported_at_once(monkeypatch, tmp_path):
    """The port is taken: the server stops, the wait ends at once and the reason is shown (not a 3-minute wait)."""
    import time
    monkeypatch.setenv("QSTS_DATABASE_URL", f"sqlite:///{tmp_path}/q.db")
    monkeypatch.setenv("QSTS_STATE_DIR", str(tmp_path / "var"))
    monkeypatch.setitem(launcher._SERVER, "thread", None)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        launcher.start_server(port)
        t0 = time.monotonic()
        assert launcher.wait_ready(f"http://127.0.0.1:{port}/nothing-here", timeout=60) is False
        assert time.monotonic() - t0 < 30
    assert launcher._SERVER["errors"], "the reason must be kept"
    page = launcher.failure_page(tmp_path, "La parte interna de QSTS no ha respondido <b>")
    assert "&lt;b&gt;" in page and "Motivo" in page
    monkeypatch.setitem(launcher._SERVER, "thread", None)


def test_a_new_start_waits_while_the_previous_one_saves_its_copy(monkeypatch, tmp_path):
    import threading
    monkeypatch.setattr(launcher, "project_root", lambda: tmp_path)
    ran = []
    monkeypatch.setattr(launcher, "_run", lambda url, port: ran.append(launcher.get_state(tmp_path)))
    other = launcher.single_instance(tmp_path)  # the previous QSTS, still saving its data copy
    launcher.set_state(tmp_path, "closing")
    threading.Timer(0.5, lambda: (launcher.set_state(tmp_path, None), other.close())).start()
    cwd = os.getcwd()
    try:
        launcher.main(port=8796, argv=[])
    finally:
        os.chdir(cwd)
    assert ran == ["opening"] and launcher.get_state(tmp_path) is None
    # while it is open (not closing), a second double-click still does nothing
    other = launcher.single_instance(tmp_path)
    launcher.set_state(tmp_path, "open")
    ran.clear()
    try:
        launcher.main(port=8796, argv=[])
    finally:
        os.chdir(cwd)
        other.close()
    assert ran == []
