"""Using QSTS on two computers through a shared (cloud-synced) folder. Two state dirs + two databases simulate the
desktop PC and the laptop; the shared folder is a temp directory (no cloud involved)."""
import gzip
import json
import sqlite3

import pytest

from qsts.app import sync
from qsts.config import Settings
from qsts.db.session import init_db, make_engine


def make_db(path, n_rows=3):
    eng = make_engine(f"sqlite:///{path}")
    init_db(eng)
    eng.dispose()
    con = sqlite3.connect(path)
    for i in range(n_rows):
        con.execute("INSERT INTO research_candidates (id, fitness, status, origin, cycle, definition, created_at) "
                    "VALUES (?, ?, 'EVALUATED', 'evolution', 1, '{}', ?)", (f"row{i}", 0.1 * i, f"2026-10-0{i + 1} 10:00:00"))
    con.commit()
    con.close()


def rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT count(*) FROM research_candidates").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def two_computers(tmp_path, monkeypatch):
    shared = tmp_path / "OneDrive" / "QSTS"
    pc = {"db": tmp_path / "pc" / "qsts.db", "state": tmp_path / "pc" / "var"}
    laptop = {"db": tmp_path / "laptop" / "qsts.db", "state": tmp_path / "laptop" / "var"}
    for c in (pc, laptop):
        c["db"].parent.mkdir(parents=True)
        sync.save_state(c["state"], dir=str(shared))
    make_db(pc["db"], 5)
    make_db(laptop["db"], 0)
    return shared, pc, laptop


def test_save_on_one_computer_load_on_the_other(two_computers, monkeypatch):
    shared, pc, laptop = two_computers
    monkeypatch.setenv("COMPUTERNAME", "PC-CASA")
    assert sync.status(pc["db"], pc["state"])["local_changed"] is True
    meta = sync.export_snapshot(pc["db"], pc["state"], code_version="abc")
    assert meta["machine"] == "PC-CASA" and meta["summary"]["strategies_tested"] == 5
    assert json.loads((shared / sync.META).read_text())["id"] == meta["id"]
    assert sync.status(pc["db"], pc["state"])["local_changed"] is False  # everything saved

    monkeypatch.setenv("COMPUTERNAME", "PORTATIL")
    st = sync.status(laptop["db"], laptop["state"])
    assert st["remote_newer"] and not st["conflict"]  # the laptop has no own work yet
    with pytest.raises(sync.SyncError):  # saving now would overwrite the PC's work
        sync.export_snapshot(laptop["db"], laptop["state"])
    sync.stage_import(laptop["state"])
    url = f"sqlite:///{laptop['db']}"
    assert rows(laptop["db"]) == 0  # nothing replaced while the app runs
    loaded = sync.apply_pending_import(url, laptop["state"])
    assert loaded["machine"] == "PC-CASA" and rows(laptop["db"]) == 5
    assert list((laptop["state"] / "backups").glob("qsts-antes-de-cargar-*.db"))  # old laptop data kept
    st = sync.status(laptop["db"], laptop["state"])
    assert not st["remote_newer"] and not st["local_changed"]
    assert sync.apply_pending_import(url, laptop["state"]) is None  # nothing pending any more

    # work on the laptop, close the app: its copy is saved and the PC is told to load it
    con = sqlite3.connect(laptop["db"])
    con.execute("INSERT INTO research_candidates (id, fitness, status, origin, cycle, definition, created_at) "
                "VALUES ('new', 0.9, 'EVALUATED', 'ai', 2, '{}', '2026-10-09 10:00:00')")
    con.commit()
    con.close()
    assert sync.auto_save_on_close(url, laptop["state"]).startswith("copia guardada")
    monkeypatch.setenv("COMPUTERNAME", "PC-CASA")
    assert sync.status(pc["db"], pc["state"])["remote_newer"] is True
    assert "no se sobrescribe" in sync.auto_save_on_close(f"sqlite:///{pc['db']}", pc["state"])


def test_conflict_and_incomplete_copies_are_detected(two_computers):
    shared, pc, laptop = two_computers
    sync.export_snapshot(pc["db"], pc["state"])
    make_db(laptop["db"], 2)  # the laptop also worked without loading the PC's copy
    st = sync.status(laptop["db"], laptop["state"])
    assert st["remote_newer"] and st["local_changed"] and st["conflict"]
    with gzip.open(shared / sync.SNAPSHOT, "ab") as g:  # OneDrive still uploading: content != announced hash
        g.write(b"partial")
    with pytest.raises(sync.SyncError):
        sync.stage_import(laptop["state"])
    assert not (laptop["state"] / sync.PENDING).exists()


def test_build_context_applies_a_staged_copy(two_computers, tmp_path):
    from qsts.app.context import build_context
    shared, pc, laptop = two_computers
    con = sqlite3.connect(pc["db"])
    con.execute("DROP TABLE paper_notifications")  # the PC runs an older version (fewer tables)
    con.close()
    sync.export_snapshot(pc["db"], pc["state"])
    sync.stage_import(laptop["state"])
    ctx = build_context(Settings(_env_file=None, database_url=f"sqlite:///{laptop['db']}", state_dir=laptop["state"]))
    assert ctx.extra["sync_loaded"]["summary"]["strategies_tested"] == 5 and rows(laptop["db"]) == 5
    st = sync.status(laptop["db"], laptop["state"])  # the schema update after loading is not "new work"
    assert st["local_changed"] is False and st["remote_newer"] is False


def test_launcher_saves_on_close_and_restarts_after_loading(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from qsts.app import launcher
    calls = []
    monkeypatch.setattr("qsts.app.sync.auto_save_on_close", lambda url, state, cv=None: calls.append("save") or "ok")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args, cwd=None: calls.append(("restart", args[-1])))
    ctx = SimpleNamespace(settings=SimpleNamespace(database_url="sqlite:///x.db", state_dir=tmp_path), extra={})
    launcher.after_close(None)  # another process owns the data: nothing to do
    launcher.after_close(ctx)
    launcher._RESTART.set()
    try:
        launcher.after_close(ctx)
    finally:
        launcher._RESTART.clear()
    assert calls == ["save", ("restart", "--wait-free")]


def test_sync_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from qsts.api.server import create_app
    from qsts.app.context import build_context
    st = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/q.db", state_dir=tmp_path / "var")
    ctx = build_context(st)
    c = TestClient(create_app(ctx))
    assert c.get("/api/sync").json()["enabled"] is False
    assert c.post("/api/sync/enable", json={"dir": str(tmp_path / "nope" / "deeper" / "QSTS")}).status_code == 400
    (tmp_path / "OneDrive").mkdir()
    s = c.post("/api/sync/enable", json={"dir": str(tmp_path / "OneDrive" / "QSTS")}).json()
    assert s["enabled"] is True and s["remote"] is None
    meta = c.post("/api/sync/save", json={}).json()
    assert (tmp_path / "OneDrive" / "QSTS" / sync.SNAPSHOT).exists() and meta["sha256"]
    restarted = []
    ctx.extra["restart_app"] = lambda: restarted.append(True)
    sync.save_state(st.state_dir, last_id="older")  # pretend the copy came from another computer
    assert c.get("/api/sync").json()["remote_newer"] is True
    assert c.post("/api/autoresearch/start", json={"use_ai": False}).status_code in (400, 409)
    r = c.post("/api/sync/load").json()
    assert r["restarting"] is True and restarted and (tmp_path / "var" / sync.PENDING).exists()
