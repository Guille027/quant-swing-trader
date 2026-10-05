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
    from qsts.research.experiments import code_version
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
