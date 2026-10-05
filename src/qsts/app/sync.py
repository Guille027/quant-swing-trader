"""Use QSTS on several computers (e.g. desktop PC and laptop) through a cloud-synced folder such as OneDrive.

Model: ONE computer at a time. The whole database is saved as a single compressed copy in the shared folder
(when the app is closed, or with a button) and the other computer loads it (replacing its own data, which is first
backed up locally). Nothing is merged: two computers working at the same time would produce two different
histories, so the app warns before overwriting work that has not been loaded yet.

Files in the shared folder: `qsts-datos.db.gz` (consistent SQLite snapshot, gzip) and `qsts-datos.json`
(who saved it, when, sha256 of the .gz). The .json is written last and the hash is checked before loading, so a
copy that is still uploading is never loaded half-way. Secrets (.env) are never copied.
Local state lives in `<state_dir>/sync_state.json`; a loaded copy is staged as `<state_dir>/qsts.db.import` and
swapped in by `apply_pending_import()` the next time the app starts, before the database is opened.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT, META, STATE, PENDING = "qsts-datos.db.gz", "qsts-datos.json", "sync_state.json", "qsts.db.import"
KEEP_BACKUPS = 5


class SyncError(RuntimeError):
    pass


def machine_name() -> str:
    return os.environ.get("COMPUTERNAME") or platform.node() or "este ordenador"


def suggested_dir(home: Path | None = None, env: dict | None = None) -> str | None:
    """OneDrive folder of THIS computer's user (+ \\QSTS). Each computer has its own path (the Windows user name
    usually differs), so the PC's path must not be typed on the laptop."""
    env = os.environ if env is None else env
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        p = env.get(var)
        if p and Path(p).is_dir():
            return str(Path(p) / "QSTS")
    home = home or Path.home()
    for cand in [home / "OneDrive", *sorted(home.glob("OneDrive*"))]:  # e.g. "OneDrive - Personal"
        if cand.is_dir():
            return str(cand / "QSTS")
    return None


def check_dir(raw: str) -> Path:
    """Validates the folder typed by the user; the error says what to do."""
    txt = (raw or "").strip().strip('"').strip()
    sug = suggested_dir()
    if not txt:
        raise SyncError("escribe la carpeta" + (f" (en este ordenador: {sug})" if sug else ""))
    folder = Path(txt)
    if not folder.is_absolute():
        raise SyncError("escribe la ruta completa de la carpeta" + (f", por ejemplo {sug}" if sug else ""))
    if not folder.parent.exists():
        msg = f"la carpeta {folder.parent} no existe en este ordenador."
        msg += (f" Aquí la carpeta de OneDrive es: {sug}" if sug else
                " No encuentro OneDrive en este ordenador: abre OneDrive (la nube junto al reloj) e inicia sesión con "
                "la misma cuenta que en el otro ordenador; luego vuelve a abrir QSTS.")
        raise SyncError(msg)
    return folder


def db_file(database_url: str) -> Path | None:
    if database_url.startswith("sqlite:///") and database_url != "sqlite:///:memory:":
        return Path(database_url.removeprefix("sqlite:///"))
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------- local state
def load_state(state_dir) -> dict:
    return _read_json(Path(state_dir) / STATE) or {}


def save_state(state_dir, **updates) -> dict:
    st = {**load_state(state_dir), **updates}
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    _write_json(Path(state_dir) / STATE, st)
    return st


SIGNATURE_QUERIES = (
    "SELECT count(*), max(created_at) FROM research_candidates",
    "SELECT count(*) FROM strategy_status_history",
    "SELECT count(*) FROM oos_access_log",
    "SELECT count(*), max(recorded_at) FROM paper_days",
    "SELECT count(*), max(sent_at) FROM paper_notifications",
    "SELECT count(*), max(stopped_at) FROM paper_sessions",
    "SELECT count(*), max(ts), max(ingested_at) FROM prices",
    "SELECT count(*), max(fetched_at) FROM earnings_events",
    "SELECT count(*) FROM corporate_actions",
    "SELECT count(*) FROM experiments",
)


def signature(db: Path) -> str:
    """Cheap fingerprint of the user's work in the database (changes when anything is added)."""
    if not db.exists():
        return "empty"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    out = []
    try:
        for q in SIGNATURE_QUERIES:
            try:
                r = con.execute(q).fetchone()
            except sqlite3.Error:  # table not created yet (older version)
                r = None
            out.append(r if r and r[0] else None)  # missing table == empty table (a newer version adds tables)
    finally:
        con.close()
    return hashlib.sha256(repr(out).encode()).hexdigest()[:24]


def summary(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    def one(q):
        try:
            return con.execute(q).fetchone()[0]
        except sqlite3.Error:
            return None
    try:
        return {"strategies_tested": one("SELECT count(*) FROM research_candidates"),
                "stocks": one("SELECT count(DISTINCT asset_id) FROM prices"),
                "last_price": one("SELECT max(ts) FROM prices"),
                "simulation_active": bool(one("SELECT count(*) FROM paper_sessions WHERE status = 'ACTIVE'"))}
    finally:
        con.close()


def _has_work(db: Path) -> bool:
    s = summary(db) if db.exists() else {}
    return bool(s.get("strategies_tested") or s.get("stocks"))


# ---------------------------------------------------------------------- status
def status(db: Path, state_dir) -> dict:
    st = load_state(state_dir)
    folder = st.get("dir")
    remote = _read_json(Path(folder) / META) if folder else None
    if st.get("last_signature") is None:
        local_changed = _has_work(db)
    else:
        local_changed = signature(db) != st.get("last_signature")
    remote_newer = bool(remote) and remote.get("id") != st.get("last_id")
    local = summary(db) if db.exists() else {}
    rs = (remote or {}).get("summary") or {}
    # loading a copy with clearly less work than this computer has is almost always a mistake (e.g. an empty copy)
    remote_smaller = bool(remote) and ((rs.get("strategies_tested") or 0) < (local.get("strategies_tested") or 0)
                                       or (rs.get("stocks") or 0) < (local.get("stocks") or 0))
    # cloud tools keep both versions when two computers write the same file: "qsts-datos-PC.db.gz", "(1)"...
    extra = sorted(p.name for p in Path(folder).glob("qsts-datos*") if p.name not in (SNAPSHOT, META)
                   and not p.name.endswith(".part")) if folder and Path(folder).is_dir() else []
    return {"enabled": bool(folder), "dir": folder, "suggested_dir": suggested_dir(), "machine": machine_name(),
            "remote": remote, "remote_newer": remote_newer, "local_changed": local_changed,
            "conflict": remote_newer and local_changed, "last": {k: st.get(k) for k in ("last_at", "last_action")},
            "pending_import": (Path(state_dir) / PENDING).exists(), "local_summary": local,
            "remote_smaller": remote_smaller, "conflict_files": extra}


# ---------------------------------------------------------------------- save (export)
def export_snapshot(db: Path, state_dir, *, code_version: str | None = None, force: bool = False) -> dict:
    st = status(db, state_dir)
    if not st["enabled"]:
        raise SyncError("la copia entre ordenadores no está activada")
    if not _has_work(db):  # an empty computer must never replace a real copy
        raise SyncError("este ordenador todavía no tiene datos: no hay nada que guardar (si los datos están en el "
                        "otro ordenador, guarda allí la copia y cárgala aquí)")
    if st["remote_newer"] and not force:
        raise SyncError(f"en la carpeta hay una copia de {st['remote'].get('machine')} ({st['remote'].get('saved_at')}) "
                        "que este ordenador no ha cargado: si guardas, se perdería")
    meta = _write_snapshot(db, Path(st["dir"]), state_dir, code_version)
    save_state(state_dir, last_id=meta["id"], last_signature=signature(db), last_at=meta["saved_at"],
               last_action="guardada")
    return meta


def _write_snapshot(db: Path, folder: Path, state_dir, code_version: str | None) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    tmp_db = Path(state_dir) / "sync-export.tmp.db"
    tmp_db.unlink(missing_ok=True)
    src, dst = sqlite3.connect(db), sqlite3.connect(tmp_db)
    try:
        src.backup(dst)  # consistent copy even while the app is writing (WAL)
    finally:
        dst.close()
        src.close()
    try:
        part = folder / (SNAPSHOT + ".part")
        with open(tmp_db, "rb") as f, gzip.open(part, "wb", compresslevel=1) as g:
            shutil.copyfileobj(f, g, 1 << 20)
        sha = _sha256(part)
        os.replace(part, folder / SNAPSHOT)
        meta = {"id": uuid.uuid4().hex, "machine": machine_name(), "saved_at": _now(), "sha256": sha,
                "size": (folder / SNAPSHOT).stat().st_size, "code_version": code_version, "summary": summary(tmp_db)}
        _write_json(folder / META, meta)  # last: the other computer only sees a complete copy
    finally:
        tmp_db.unlink(missing_ok=True)
    return meta


# ---------------------------------------------------------------------- copy as a plain file (no sync client)
def downloads_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Downloads"  # shown as "Descargas" on a Spanish Windows


def find_downloaded(folder: Path | None = None) -> dict | None:
    """Newest copy downloaded by hand (e.g. from onedrive.live.com), if any: browsers rename repeats '... (1)'."""
    folder = folder or downloads_dir()
    files = sorted((p for p in folder.glob("qsts-datos*.db.gz") if p.is_file()), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    p = files[-1]
    return {"path": str(p), "name": p.name, "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}


def export_to_file(db: Path, state_dir, folder: Path | None = None, code_version: str | None = None) -> dict:
    """Saves the copy as a file (default: Downloads) to carry it by hand: upload it on the web, a USB stick..."""
    if not _has_work(db):
        raise SyncError("este ordenador todavía no tiene datos: no hay nada que guardar")
    folder = folder or downloads_dir()
    meta = _write_snapshot(db, folder, state_dir, code_version)
    save_state(state_dir, last_id=meta["id"], last_signature=signature(db), last_at=meta["saved_at"],
               last_action="guardada en archivo")
    return {**meta, "path": str(folder / SNAPSHOT)}


# ---------------------------------------------------------------------- load (import)
def stage_import(state_dir) -> dict:
    """Downloads/decompresses the shared copy next to the local database; it replaces it at the next start."""
    st = load_state(state_dir)
    folder = Path(st.get("dir") or "")
    meta = _read_json(folder / META) if st.get("dir") else None
    if not meta:
        raise SyncError("no hay ninguna copia en la carpeta")
    gz = folder / SNAPSHOT
    if not gz.exists() or _sha256(gz) != meta.get("sha256"):
        raise SyncError("la copia todavía se está sincronizando (OneDrive no la ha terminado de bajar): "
                        "espera un poco y vuelve a intentarlo")
    _stage(gz, state_dir, meta)
    return meta


def stage_import_file(path, state_dir) -> dict:
    """Loads a copy downloaded by hand. gzip's own checksum detects an incomplete download; when the matching
    qsts-datos.json was downloaded too, its identity is kept (the copy is then known as loaded)."""
    gz = Path(str(path).strip().strip('"'))
    if not gz.is_file():
        raise SyncError(f"no encuentro el archivo {gz}")
    side = _read_json(gz.with_name(META))
    meta = side if side and side.get("sha256") == _sha256(gz) else None
    staged_meta = meta or {"id": None, "machine": f"archivo {gz.name}", "sha256": _sha256(gz),
                           "saved_at": datetime.fromtimestamp(gz.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
    _stage(gz, state_dir, staged_meta)
    return staged_meta


def _stage(gz: Path, state_dir, meta: dict) -> None:
    staged = Path(state_dir) / PENDING
    part = staged.with_name(staged.name + ".part")
    try:
        with gzip.open(gz, "rb") as g, open(part, "wb") as f:
            shutil.copyfileobj(g, f, 1 << 20)
    except (OSError, EOFError) as e:  # truncated / corrupt download
        part.unlink(missing_ok=True)
        raise SyncError(f"el archivo está incompleto o dañado ({e.__class__.__name__}): vuelve a descargarlo") from None
    con = sqlite3.connect(part)
    try:
        ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.DatabaseError:  # not a database at all
        ok, tables = False, set()
    finally:
        con.close()
    if not ok or "research_candidates" not in tables:
        part.unlink(missing_ok=True)
        raise SyncError("la copia está dañada; vuelve a guardarla desde el otro ordenador")
    if meta.get("summary") is None:
        meta["summary"] = summary(part)
    os.replace(part, staged)
    _write_json(Path(state_dir) / (PENDING + ".json"), meta)


def discard_pending(state_dir) -> None:
    for name in (PENDING, PENDING + ".json"):
        (Path(state_dir) / name).unlink(missing_ok=True)


def smaller_than_local(remote_summary: dict | None, db: Path) -> bool:
    rs, local = remote_summary or {}, summary(db) if db.exists() else {}
    return ((rs.get("strategies_tested") or 0) < (local.get("strategies_tested") or 0)
            or (rs.get("stocks") or 0) < (local.get("stocks") or 0))


def apply_pending_import(database_url: str, state_dir) -> dict | None:
    """At startup, BEFORE the database is opened: swap in a staged copy (the current data is backed up first)."""
    db = db_file(database_url)
    staged = Path(state_dir) / PENDING
    if db is None or not staged.exists():
        return None
    meta = _read_json(Path(state_dir) / (PENDING + ".json")) or {}
    backups = Path(state_dir) / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    if db.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        src, dst = sqlite3.connect(db), sqlite3.connect(backups / f"qsts-antes-de-cargar-{stamp}.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        for old in sorted(backups.glob("qsts-antes-de-cargar-*.db"))[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, db)
    (Path(state_dir) / (PENDING + ".json")).unlink(missing_ok=True)
    save_state(state_dir, last_id=meta.get("id"), last_signature=signature(db), last_at=_now(),
               last_action=f"cargada de {meta.get('machine', '?')}")
    return meta


def auto_save_on_close(database_url: str, state_dir, code_version: str | None = None) -> str:
    """Called when the app window closes: save the copy if this computer has new work and nothing newer waits."""
    db = db_file(database_url)
    if db is None:
        return "sin base de datos SQLite"
    st = status(db, state_dir)
    if not st["enabled"]:
        return "copia entre ordenadores desactivada"
    if st["pending_import"]:
        return "hay una copia pendiente de cargar: no se guarda"
    if st["remote_newer"]:
        return "la carpeta tiene una copia más nueva de otro ordenador: no se sobrescribe"
    if not st["local_changed"]:
        return "sin cambios desde la última copia"
    if not _has_work(db):
        return "este ordenador no tiene datos: no se guarda nada"
    meta = export_snapshot(db, state_dir, code_version=code_version)
    return f"copia guardada ({meta['size'] / 1e6:.0f} MB)"
