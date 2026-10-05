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
    assert calls == ["save", ("restart", str(__import__("os").getpid()))]  # the new one waits for this process


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
    assert c.post("/api/sync/save", json={}).status_code == 409  # nothing to save on an empty computer
    con = sqlite3.connect(tmp_path / "q.db")
    con.execute("INSERT INTO research_candidates (id, fitness, status, origin, cycle, definition, created_at) "
                "VALUES ('x', 0.5, 'EVALUATED', 'evolution', 1, '{}', '2026-10-01 10:00:00')")
    con.commit()
    con.close()
    meta = c.post("/api/sync/save", json={}).json()
    assert (tmp_path / "OneDrive" / "QSTS" / sync.SNAPSHOT).exists() and meta["sha256"]
    restarted = []
    ctx.extra["restart_app"] = lambda: restarted.append(True)
    sync.save_state(st.state_dir, last_id="older")  # pretend the copy came from another computer
    assert c.get("/api/sync").json()["remote_newer"] is True
    assert c.post("/api/autoresearch/start", json={"use_ai": False}).status_code in (400, 409)
    con = sqlite3.connect(tmp_path / "q.db")  # meanwhile this computer did more work than the copy has
    con.execute("INSERT INTO research_candidates (id, fitness, status, origin, cycle, definition, created_at) "
                "VALUES ('y', 0.6, 'EVALUATED', 'evolution', 2, '{}', '2026-10-02 10:00:00')")
    con.commit()
    con.close()
    refused = c.post("/api/sync/load", json={})
    assert refused.status_code == 409 and "MENOS datos" in refused.json()["detail"] and not restarted
    r = c.post("/api/sync/load", json={"force": True}).json()
    assert r["restarting"] is True and restarted and (tmp_path / "var" / sync.PENDING).exists()


def test_onedrive_folder_is_this_computers_own(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "portatil"
    (home / "OneDrive - Personal").mkdir(parents=True)
    assert sync.suggested_dir(home=home, env={}) == str(home / "OneDrive - Personal" / "QSTS")
    env_dir = tmp_path / "od"
    env_dir.mkdir()
    assert sync.suggested_dir(home=home, env={"OneDrive": str(env_dir)}) == str(env_dir / "QSTS")
    assert sync.suggested_dir(home=tmp_path / "nobody", env={}) is None
    monkeypatch.setattr(sync, "suggested_dir", lambda: str(home / "OneDrive - Personal" / "QSTS"))
    with pytest.raises(sync.SyncError) as e:  # the PC's path typed on the laptop
        sync.check_dir(str(tmp_path / "Users" / "pc" / "OneDrive" / "QSTS"))
    assert "no existe en este ordenador" in str(e.value) and "OneDrive - Personal" in str(e.value)
    with pytest.raises(sync.SyncError):
        sync.check_dir("  ")
    assert sync.check_dir(f'"{home / "OneDrive - Personal" / "QSTS"}"') == home / "OneDrive - Personal" / "QSTS"


def test_an_empty_computer_never_replaces_a_real_copy(two_computers, monkeypatch):
    shared, pc, laptop = two_computers
    sync.export_snapshot(pc["db"], pc["state"])  # the PC's real copy (5 strategies)
    meta_pc = json.loads((shared / sync.META).read_text())
    with pytest.raises(sync.SyncError, match="no tiene datos"):  # even when forced
        sync.export_snapshot(laptop["db"], laptop["state"], force=True)
    assert "no tiene datos" in sync.auto_save_on_close(f"sqlite:///{laptop['db']}", laptop["state"]) or \
        "no se sobrescribe" in sync.auto_save_on_close(f"sqlite:///{laptop['db']}", laptop["state"])
    assert json.loads((shared / sync.META).read_text())["id"] == meta_pc["id"]  # untouched
    # a smaller copy (e.g. made on a computer with less data) is flagged before loading
    make_db(laptop["db"], 9)
    sync.save_state(laptop["state"], last_id="something-else")
    st = sync.status(laptop["db"], laptop["state"])
    assert st["remote_newer"] and st["remote_smaller"] and st["local_summary"]["strategies_tested"] == 9 + 0
    (shared / "qsts-datos-PORTATIL.db.gz").write_bytes(b"x")  # what OneDrive does with conflicting writes
    assert sync.status(laptop["db"], laptop["state"])["conflict_files"] == ["qsts-datos-PORTATIL.db.gz"]


def test_copy_carried_as_a_downloaded_file(two_computers, tmp_path):
    shared, pc, laptop = two_computers
    pc_downloads, lap_downloads = tmp_path / "pc_dl", tmp_path / "lap_dl"
    meta = sync.export_to_file(pc["db"], pc["state"], pc_downloads)  # no sync folder needed
    assert meta["path"].endswith(sync.SNAPSHOT) and meta["summary"]["strategies_tested"] == 5
    lap_downloads.mkdir()
    # the browser renames repeated downloads; the .json may or may not come along
    (lap_downloads / "qsts-datos (1).db.gz").write_bytes((pc_downloads / sync.SNAPSHOT).read_bytes())
    found = sync.find_downloaded(lap_downloads)
    assert found["name"] == "qsts-datos (1).db.gz"
    staged = sync.stage_import_file(found["path"], laptop["state"])
    assert staged["machine"].startswith("archivo") and staged["summary"]["strategies_tested"] == 5
    sync.apply_pending_import(f"sqlite:///{laptop['db']}", laptop["state"])
    assert rows(laptop["db"]) == 5
    # with its .json next to it, the copy keeps its identity
    (lap_downloads / sync.META).write_text((pc_downloads / sync.META).read_text())
    (lap_downloads / sync.SNAPSHOT).write_bytes((pc_downloads / sync.SNAPSHOT).read_bytes())
    assert sync.stage_import_file(lap_downloads / sync.SNAPSHOT, laptop["state"])["id"] == meta["id"]
    sync.discard_pending(laptop["state"])
    broken = lap_downloads / "qsts-datos (2).db.gz"  # interrupted download
    broken.write_bytes((pc_downloads / sync.SNAPSHOT).read_bytes()[:2000])
    with pytest.raises(sync.SyncError, match="incompleto"):
        sync.stage_import_file(broken, laptop["state"])
    with pytest.raises(sync.SyncError):
        sync.export_to_file(tmp_path / "empty.db", laptop["state"], lap_downloads)


def test_file_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from qsts.api.server import create_app
    from qsts.app.context import build_context
    st = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/q.db", state_dir=tmp_path / "var")
    ctx = build_context(st)
    ctx.extra["downloads_dir"] = tmp_path / "Downloads"
    (tmp_path / "Downloads").mkdir()
    c = TestClient(create_app(ctx))
    assert c.get("/api/sync").json()["downloaded"] is None
    assert c.post("/api/sync/load_file", json={}).status_code == 400
    con = sqlite3.connect(tmp_path / "q.db")
    con.execute("INSERT INTO research_candidates (id, fitness, status, origin, cycle, definition, created_at) "
                "VALUES ('x', 0.5, 'EVALUATED', 'evolution', 1, '{}', '2026-10-01 10:00:00')")
    con.commit()
    con.close()
    saved = c.post("/api/sync/save_file").json()
    assert saved["path"] == str(tmp_path / "Downloads" / sync.SNAPSHOT)
    assert c.get("/api/sync").json()["downloaded"]["name"] == sync.SNAPSHOT
    restarted = []
    ctx.extra["restart_app"] = lambda: restarted.append(True)
    assert c.post("/api/sync/load_file", json={}).json()["restarting"] is True and restarted


def test_locked_files_leave_everything_as_it_was(two_computers, monkeypatch):
    shared, pc, laptop = two_computers
    sync.export_snapshot(pc["db"], pc["state"])
    sync.stage_import(laptop["state"])
    real_replace = sync.os.replace
    def locked(src, dst):  # Windows: the previous QSTS still has the database open
        if str(src).endswith(sync.PENDING):
            raise PermissionError(13, "El proceso no tiene acceso al archivo porque está siendo utilizado por otro proceso")
        return real_replace(src, dst)
    monkeypatch.setattr(sync.os, "replace", locked)
    with pytest.raises(sync.SyncError, match="No se ha perdido nada"):
        sync.apply_pending_import(f"sqlite:///{laptop['db']}", laptop["state"], wait_s=0.2)
    assert laptop["db"].exists() and rows(laptop["db"]) == 0 and (laptop["state"] / sync.PENDING).exists()
    monkeypatch.setattr(sync.os, "replace", real_replace)  # next start: the lock is gone
    assert sync.apply_pending_import(f"sqlite:///{laptop['db']}", laptop["state"])["summary"]["strategies_tested"] == 5
    assert rows(laptop["db"]) == 5 and not list(laptop["db"].parent.glob("*.old"))


def test_restart_waits_for_the_previous_process():
    import subprocess as sp
    import time
    from qsts.app import launcher
    p = sp.Popen([__import__("sys").executable, "-c", "import time; time.sleep(0.5)"])
    t = time.monotonic()
    launcher.wait_for_exit(p.pid, timeout=2)  # (on Linux a finished child stays a zombie until reaped)
    assert time.monotonic() - t >= 0.3 and p.wait() == 0
