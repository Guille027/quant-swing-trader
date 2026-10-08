"""Local backend API for the desktop app (FastAPI). Binds to 127.0.0.1 only.

The UI is a thin client: every number it shows comes from these endpoints. Main parts: the strategy library
(backtests of bots), automatic paper trading on the Alpaca paper account, market data downloads, Telegram, and
the data copy shared between two computers.
"""
from __future__ import annotations

import math
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, SecretStr

from qsts.app import sync
from qsts.app.context import AppContext
from qsts.app.datajobs import DataJobRunner, sample_symbols
from qsts.app.envfile import set_env_values
from qsts.broker.alpaca_paper import AlpacaPaper, BrokerError
from qsts.data.universe import UniverseList, fetch_sp500
from qsts.lab import metrics as lab_metrics
from qsts.lab.service import CAPITAL, LabService
from qsts.lab.strategy import REGISTRY
from qsts.lab.trader import PaperTrader
from qsts.notify.telegram import Telegram, TelegramError

STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"
ETFS = ("SPY", "QQQ", "IWM", "DIA", "GLD", "TLT")


def _j(x):
    """JSON-safe conversion (NaN/inf -> None, numpy -> python, timestamps -> iso)."""
    if isinstance(x, dict):
        return {str(k): _j(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_j(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    return x


class IngestBody(BaseModel):
    mode: str  # sp500 | symbols | update
    symbols: list[str] = []
    sample: int | None = None  # sp500: random sample size (None = all)
    start: str = "2010-01-01"


class BotBody(BaseModel):
    strategy: str
    symbol: str
    params: dict = {}


class BotUpdateBody(BaseModel):
    favorite: bool | None = None
    hidden: bool | None = None
    size_pct: float | None = None


class ActivateBody(BaseModel):
    allocation_pct: float = 20.0
    follow_open: bool = True


class DeactivateBody(BaseModel):
    close: bool = True


class AlpacaKeysBody(BaseModel):
    key: str
    secret: str


class TelegramTokenBody(BaseModel):
    token: str


class SyncDirBody(BaseModel):
    dir: str


class SyncSaveBody(BaseModel):
    force: bool = False


class SyncLoadBody(BaseModel):
    force: bool = False  # load even though the copy has less data than this computer
    path: str | None = None  # load_file: a copy downloaded by hand (default: newest in Downloads)


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        if ctx.extra.get("trader_autostart", True):
            _trader().start()  # paper trading of the active bots, every couple of minutes while the app is open
        yield
        tr = ctx.extra.get("trader")
        if tr is not None:
            tr.stop()

    app = FastAPI(title="QSTS", docs_url="/api/docs", lifespan=lifespan)
    if "code_version" not in ctx.extra:
        from qsts.core.version import code_version
        ctx.extra["code_version"] = code_version()

    @app.middleware("http")
    async def _no_stale_ui(request, call_next):
        # after an update the window must load the new screens, never a cached copy
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------ system
    @app.post("/api/shutdown")
    def shutdown():
        stop = ctx.extra.get("shutdown")
        if stop is None:
            raise HTTPException(409, "este servidor no admite apagado remoto")
        stop()
        return {"stopping": True}

    @app.get("/api/ping")
    def ping():
        """Instant 'I am up' for the launcher (no database work)."""
        return {"ok": True, "code_version": ctx.extra.get("code_version")}

    @app.get("/api/status")
    def status():
        tr = _trader()
        return _j({"code_version": ctx.extra.get("code_version"), "alpaca": _alpaca_configured(),
                   "telegram": _telegram() is not None, "trader": tr.state,
                   "paper_bots": len(tr.active_bots()), "data_job": _data_runner().state.running})

    @app.post("/api/stop-all")
    def stop_all(body: DeactivateBody):
        """Emergency stop: every bot leaves paper trading (and closes its position at the next open if asked)."""
        tr, out = _trader(), []
        for b in tr.active_bots():
            try:
                out.append({"bot": b.id, **tr.deactivate(b.id, close=body.close)})
            except ValueError as e:
                out.append({"bot": b.id, "error": str(e)})
        return _j({"stopped": out})

    # ------------------------------------------------------------------ strategy library
    def _basket() -> list[str]:
        have = set(ctx.symbols())
        return [s for s in ETFS if s in have] + [s for s in ctx.repo.liquid_symbols(30) if s not in ETFS]

    def _lab() -> LabService:
        lab = ctx.extra.get("lab")
        if lab is None:
            lab = ctx.extra["lab"] = LabService(ctx.sf, ctx.research_frame, ctx.repo.data_token, basket=_basket,
                                                benchmark=ctx.settings.benchmark)
            lab.sync()
        return lab

    @app.get("/api/library")
    def library():
        lib = _lab().library()
        lib["missing"] = sorted({r["symbol"] for r in lib["rows"] if r.get("error", "").startswith("sin datos")})
        return _j(lib)

    @app.get("/api/strategies")
    def strategies():
        return _j([REGISTRY[k].info() for k in sorted(REGISTRY)])

    @app.post("/api/bots")
    def create_bot(body: BotBody):
        lab = _lab()
        try:
            b = lab.create_bot(body.strategy, body.symbol, body.params)
        except KeyError:
            raise HTTPException(404, "estrategia desconocida")
        except ValueError as e:
            raise HTTPException(400, str(e))
        downloading = False
        if b.symbol not in set(ctx.symbols()):  # no prices yet: download them now
            downloading = _data_runner().start("symbols", [b.symbol], "2010-01-01", earnings=False)
        return {"id": b.id, "downloading": downloading}

    @app.post("/api/bots/{bid}/update")
    def update_bot(bid: str, body: BotUpdateBody):
        try:
            _lab().update_bot(bid, **body.model_dump())
        except KeyError:
            raise HTTPException(404, "bot desconocido")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.get("/api/bots/{bid}")
    def bot_detail(bid: str):
        lab, tr = _lab(), _trader()
        try:
            d = lab.detail(bid)
        except KeyError:
            raise HTTPException(404, "bot desconocido")
        except ValueError as e:
            raise HTTPException(400, str(e))
        b = lab.get_bot(bid)
        d["paper"] = _paper_view(b, d) if b.activated_at else None
        return _j(d)

    def _paper_view(b, d: dict) -> dict:
        """The bot's paper results since activation, also as a continuation of the backtest curve (in %)."""
        tr = _trader()
        _res, bars = _lab().result(b)
        led = tr.ledger(b)
        curve = tr.live_curve(b, bars)
        live, metrics = [], {}
        if len(curve) and b.capital:
            start = int(curve.index[0].timestamp())
            before = [p for p in d["equity"] if p["time"] <= start]
            base = before[-1]["value"] if before else 0.0
            live = [{"time": int(t.timestamp()), "value": round(((1 + base / 100) * v / b.capital - 1) * 100, 4)}
                    for t, v in curve.items()]
            trades = pd.DataFrame(led["trades"]) if led["trades"] else pd.DataFrame(
                columns=["pnl", "pnl_pct", "side", "bars"])
            if "bars" not in trades:
                trades["bars"] = np.nan
            st = lab_metrics.trade_stats(trades)
            metrics = {"net_profit_pct": float(curve.iloc[-1] / b.capital - 1), "win_rate": st["win_rate"],
                       "profit_factor": st["profit_factor"], "n_trades": st["n_trades"],
                       "max_drawdown": float(lab_metrics.drawdown(curve).min()),
                       "d7": lab_metrics.window_return(curve, 7), "d30": lab_metrics.window_return(curve, 30),
                       "d90": lab_metrics.window_return(curve, 90)}
        return {"status": b.paper_status, "capital": b.capital, "allocation_pct": b.allocation_pct,
                "activated_at": b.activated_at.isoformat(timespec="minutes"),
                "position": led["qty"], "avg_price": led["avg"], "realized": led["realized"],
                "stop": b.stop_level, "target": b.target_level, "pending": b.pending, "live": live,
                "metrics": metrics, "trades": led["trades"][::-1][:200], "events": tr.events(b.id, 50),
                "orders": [{"id": o.id, "purpose": o.purpose, "side": o.side, "qty": o.qty, "type": o.order_type,
                            "status": o.status, "filled_price": o.filled_price, "stop": o.stop_price,
                            "limit": o.limit_price, "submitted": o.submitted_at.isoformat(timespec="minutes"),
                            "filled": o.filled_at.isoformat(timespec="minutes") if o.filled_at else None,
                            "error": o.error} for o in tr.orders(b.id)[::-1][:50]]}

    @app.get("/api/bots/{bid}/audit")
    def bot_audit(bid: str):
        try:
            return _j(_lab().audit(bid))
        except KeyError:
            raise HTTPException(404, "bot desconocido")
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------------ paper trading (Alpaca)
    def _alpaca_configured() -> bool:
        return bool(ctx.settings.alpaca_api_key and ctx.settings.alpaca_secret_key)

    def _broker():
        factory = ctx.extra.get("broker_factory")
        if factory is not None:
            return factory()
        if not _alpaca_configured():
            return None
        key = (ctx.settings.alpaca_api_key.get_secret_value(), ctx.settings.alpaca_secret_key.get_secret_value())
        cached = ctx.extra.get("broker")
        if cached is None or cached[0] != key:
            cached = ctx.extra["broker"] = (key, AlpacaPaper(*key))
        return cached[1]

    def _trader() -> PaperTrader:
        tr = ctx.extra.get("trader")
        if tr is None:
            tr = ctx.extra["trader"] = PaperTrader(
                ctx.sf, _lab(), _broker, _telegram,
                refresh=lambda syms: _data_runner().start("bots", syms, "2010-01-01", incremental=True,
                                                         earnings=False),
                data_busy=lambda: _data_runner().state.running, delay_min=ctx.settings.daily_report_delay_min,
                blocked=_sync_block_reason)
        return tr

    @app.get("/api/alpaca")
    def alpaca_status():
        if not _alpaca_configured() and ctx.extra.get("broker_factory") is None:
            return {"configured": False}
        try:
            br = _broker()
            return _j({"configured": True, "account": br.account(), "clock": br.clock()})
        except BrokerError as e:
            return {"configured": True, "error": str(e)}

    @app.post("/api/alpaca/keys")
    def alpaca_keys(body: AlpacaKeysBody):
        key, secret = body.key.strip(), body.secret.strip()
        if len(key) < 10 or len(secret) < 20 or any(ch.isspace() for ch in key + secret):
            raise HTTPException(400, "eso no parecen las claves de Alpaca (copia la 'API Key' y la 'Secret Key' enteras)")
        try:
            acct = (ctx.extra.get("broker_check") or (lambda k, s: AlpacaPaper(k, s).account()))(key, secret)
        except BrokerError as e:
            raise HTTPException(400, f"Alpaca no acepta esas claves de paper trading: {e}")
        set_env_values({"QSTS_ALPACA_API_KEY": key, "QSTS_ALPACA_SECRET_KEY": secret}, ctx.extra.get("env_path", ".env"))
        ctx.settings.alpaca_api_key, ctx.settings.alpaca_secret_key = SecretStr(key), SecretStr(secret)
        ctx.extra.pop("broker", None)
        return _j({"saved": True, "account": acct})

    @app.get("/api/paper")
    def paper_overview():
        tr, lab = _trader(), _lab()
        bots = []
        for b in lab.bots(include_hidden=True):
            if not b.activated_at:
                continue
            led = tr.ledger(b)
            bots.append({"id": b.id, "name": REGISTRY[b.strategy].name, "symbol": b.symbol, "status": b.paper_status,
                         "allocation_pct": b.allocation_pct, "capital": b.capital, "position": led["qty"],
                         "realized": led["realized"], "trades": len(led["trades"]),
                         "activated_at": b.activated_at.isoformat(timespec="minutes")})
        return _j({"state": tr.state, "log": list(tr.logs)[-40:], "events": tr.events(limit=60), "bots": bots,
                   "alpaca": _alpaca_configured() or ctx.extra.get("broker_factory") is not None})

    @app.post("/api/paper/run")
    def paper_run():
        return {"state": _trader().tick()}

    @app.post("/api/bots/{bid}/activate")
    def activate(bid: str, body: ActivateBody):
        try:
            return _j(_trader().activate(bid, body.allocation_pct, body.follow_open))
        except KeyError:
            raise HTTPException(404, "bot desconocido")
        except (ValueError, BrokerError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/bots/{bid}/deactivate")
    def deactivate(bid: str, body: DeactivateBody):
        try:
            return _j(_trader().deactivate(bid, body.close))
        except KeyError:
            raise HTTPException(404, "bot desconocido")
        except (ValueError, BrokerError) as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------------ market data ("Datos")
    def _invalidate_views():
        pass  # backtests are recomputed automatically when the stored prices change (data token)

    def _yahoo():
        from qsts.data.providers.yfinance_provider import YFinanceProvider
        return YFinanceProvider()

    def _data_runner() -> DataJobRunner:
        r = ctx.extra.get("datajob")
        if r is None:
            r = ctx.extra["datajob"] = DataJobRunner(ctx.repo, ctx.extra.get("data_provider_factory") or _yahoo,
                                                     on_finish=_invalidate_views)
        return r

    def _sp500() -> UniverseList:
        cached = ctx.extra.get("sp500")
        if cached is not None and (pd.Timestamp.now(tz="UTC") - pd.Timestamp(cached.fetched_at)) < pd.Timedelta(hours=12):
            return cached
        ctx.extra["sp500"] = (ctx.extra.get("sp500_fetcher") or fetch_sp500)()
        return ctx.extra["sp500"]

    @app.get("/api/data/summary")
    def data_summary():
        rows = ctx.repo.summary()
        return _j({"count": len(rows), "symbols": rows, "benchmark": ctx.settings.benchmark,
                   "has_benchmark": any(r["symbol"] == ctx.settings.benchmark for r in rows),
                   "first": min((r["first"] for r in rows), default=None),
                   "last": max((r["last"] for r in rows), default=None)})

    @app.get("/api/data/sp500")
    def data_sp500():
        try:
            ul = _sp500()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"no se pudo descargar la lista del S&P 500: {e}")
        have = {r["symbol"] for r in ctx.repo.summary()}
        return {"source": ul.source, "fetched_at": ul.fetched_at, "count": len(ul.members), "sectors": ul.sectors(),
                "loaded": int(ul.members["symbol"].isin(have).sum())}

    @app.post("/api/data/ingest")
    def data_ingest(body: IngestBody):
        bench = ctx.settings.benchmark
        have = [r["symbol"] for r in ctx.repo.summary()]
        fields, members = {}, None
        if body.mode == "update":
            if not have:
                raise HTTPException(400, "no hay datos que actualizar")
            syms, incremental = have, True
        elif body.mode == "symbols":
            syms = sorted({x.strip().upper().replace(".", "-") for x in body.symbols if x.strip()})
            if not syms:
                raise HTTPException(400, "escribe al menos un símbolo")
            incremental = False
        elif body.mode == "sp500":
            try:
                ul = _sp500()
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, f"no se pudo descargar la lista del S&P 500: {e}")
            mem = ul.members
            syms = sample_symbols(list(mem["symbol"]), body.sample) if body.sample else sorted(mem["symbol"])
            wanted = set(syms) | set(have)
            fields = {r.symbol: {"name": r.name, "sector": r.sector} for r in mem.itertuples() if r.symbol in wanted}
            members = ("SP500", [(r.symbol, r.date_added.date(), None) for r in mem.itertuples()
                                 if pd.notna(r.date_added)], ul.source)
            incremental = False
        else:
            raise HTTPException(400, "modo desconocido")
        if bench not in syms and bench not in have:
            syms = [bench, *syms]
        started = _data_runner().start(body.mode, syms, body.start, incremental=incremental, asset_fields=fields,
                                       memberships=members, earnings=False)
        if not started:
            raise HTTPException(409, "ya hay una descarga en marcha")
        return {"started": True, "symbols": len(syms)}

    @app.get("/api/data/job")
    def data_job():
        return _j(_data_runner().status())

    @app.post("/api/data/job/stop")
    def data_job_stop():
        _data_runner().stop()
        return {"stopping": True}

    # ------------------------------------------------------------------ several computers (OneDrive copy)
    def _db():
        db = sync.db_file(ctx.settings.database_url)
        if db is None:
            raise HTTPException(400, "la copia entre ordenadores solo funciona con la base de datos SQLite local")
        return db

    def _sync_status() -> dict:
        db = sync.db_file(ctx.settings.database_url)
        return sync.status(db, ctx.settings.state_dir) if db is not None else {"enabled": False}

    def _sync_newer() -> str | None:
        try:
            st = _sync_status()
        except Exception:  # noqa: BLE001 - an unreachable folder must not block the app
            return None
        return (st["remote"] or {}).get("machine", "otro ordenador") if st.get("remote_newer") else None

    def _sync_block_reason() -> str | None:
        newer = _sync_newer()
        return f"hay datos más recientes de {newer} sin cargar" if newer else None

    def _less(rs: dict, ls: dict, who: str) -> str:
        return (f"OJO: {who} tiene MENOS datos que este ordenador ({rs.get('bots') or 0} bots, "
                f"{rs.get('paper_orders') or 0} órdenes y {rs.get('stocks') or 0} acciones, frente a {ls.get('bots') or 0}, "
                f"{ls.get('paper_orders') or 0} y {ls.get('stocks') or 0} aquí). Si lo cargas, este ordenador perdería "
                "sus datos (quedaría una copia de seguridad).")

    def _check_idle():
        if _data_runner().state.running:
            raise HTTPException(409, "espera a que termine la descarga de datos")

    @app.get("/api/sync")
    def sync_status():
        st = _sync_status()
        try:
            downloaded = sync.find_downloaded(ctx.extra.get("downloads_dir"))
        except OSError:
            downloaded = None
        return _j({**st, "loaded_at_start": ctx.extra.get("sync_loaded"), "code_version": ctx.extra.get("code_version"),
                   "downloaded": downloaded, "downloads_dir": str(ctx.extra.get("downloads_dir") or sync.downloads_dir())})

    @app.post("/api/sync/save_file")
    def sync_save_file():
        try:
            return _j(sync.export_to_file(_db(), ctx.settings.state_dir, ctx.extra.get("downloads_dir"),
                                          code_version=ctx.extra.get("code_version")))
        except (sync.SyncError, OSError) as e:
            raise HTTPException(409 if isinstance(e, sync.SyncError) else 400, str(e))

    @app.post("/api/sync/load_file")
    def sync_load_file(body: SyncLoadBody):
        _check_idle()
        path = body.path or (sync.find_downloaded(ctx.extra.get("downloads_dir")) or {}).get("path")
        if not path:
            raise HTTPException(400, "no encuentro ninguna copia (qsts-datos….db.gz) en tu carpeta de Descargas")
        try:
            meta = sync.stage_import_file(path, ctx.settings.state_dir)
        except (sync.SyncError, OSError) as e:
            raise HTTPException(409, str(e))
        rs, ls = meta.get("summary") or {}, sync.summary(_db()) if _db().exists() else {}
        if sync.smaller_than_local(rs, _db()) and not body.force:
            sync.discard_pending(ctx.settings.state_dir)
            raise HTTPException(409, _less(rs, ls, "ese archivo"))
        restart = ctx.extra.get("restart_app")
        if restart is not None:
            restart()
        return _j({"staged": meta, "restarting": restart is not None})

    @app.post("/api/sync/enable")
    def sync_enable(body: SyncDirBody):
        try:
            folder = sync.check_dir(body.dir)
            folder.mkdir(parents=True, exist_ok=True)
        except (sync.SyncError, OSError) as e:
            raise HTTPException(400, str(e))
        sync.save_state(ctx.settings.state_dir, dir=str(folder))
        return _j(_sync_status())

    @app.post("/api/sync/disable")
    def sync_disable():
        sync.save_state(ctx.settings.state_dir, dir=None)
        return _j(_sync_status())

    @app.post("/api/sync/save")
    def sync_save(body: SyncSaveBody):
        try:
            return _j(sync.export_snapshot(_db(), ctx.settings.state_dir, code_version=ctx.extra.get("code_version"),
                                           force=body.force))
        except (sync.SyncError, OSError) as e:
            raise HTTPException(409 if isinstance(e, sync.SyncError) else 400, str(e))

    @app.post("/api/sync/load")
    def sync_load(body: SyncLoadBody | None = None):
        st = _sync_status()
        if st.get("remote_smaller") and not (body and body.force):
            rs, ls = (st.get("remote") or {}).get("summary") or {}, st.get("local_summary") or {}
            raise HTTPException(409, _less(rs, ls, f"la copia de {(st.get('remote') or {}).get('machine')}"))
        _check_idle()
        try:
            meta = sync.stage_import(ctx.settings.state_dir)
        except (sync.SyncError, OSError) as e:
            raise HTTPException(409, str(e))
        restart = ctx.extra.get("restart_app")
        if restart is not None:
            restart()  # the launcher closes the window and opens QSTS again with the loaded data
        return _j({"staged": meta, "restarting": restart is not None})

    # ------------------------------------------------------------------ Telegram
    def _tg_client(token: str | None = None, with_chat: bool = True):
        tok = token or (ctx.settings.telegram_bot_token.get_secret_value() if ctx.settings.telegram_bot_token else None)
        if not tok:
            return None
        chat = ctx.settings.telegram_chat_id if with_chat else None
        return (ctx.extra.get("telegram_factory") or Telegram)(tok, chat)

    def _telegram():
        return _tg_client() if ctx.settings.telegram_chat_id else None

    def _save_env(values: dict) -> None:
        set_env_values(values, ctx.extra.get("env_path", ".env"))

    @app.get("/api/telegram")
    def tg_status():
        return _j({"token_set": ctx.settings.telegram_bot_token is not None, "chat_id": ctx.settings.telegram_chat_id,
                   "configured": _telegram() is not None})

    @app.post("/api/telegram/token")
    def tg_token(body: TelegramTokenBody):
        tok = body.token.strip()
        try:
            me = _tg_client(tok, with_chat=False).me()
        except TelegramError as e:
            raise HTTPException(400, f"Telegram no acepta ese token: {e}")
        _save_env({"QSTS_TELEGRAM_BOT_TOKEN": tok})
        ctx.settings.telegram_bot_token = SecretStr(tok)
        return {"bot": (me or {}).get("username"), "name": (me or {}).get("first_name")}

    @app.post("/api/telegram/detect")
    def tg_detect():
        tg = _tg_client(with_chat=False)
        if tg is None:
            raise HTTPException(400, "primero guarda el token del bot")
        try:
            chat = tg.find_chat()
        except TelegramError as e:
            raise HTTPException(400, str(e))
        if chat is None:
            raise HTTPException(400, "no encuentro ningún mensaje: abre tu bot en Telegram, pulsa Iniciar (o escríbele "
                                     "cualquier cosa) y vuelve a pulsar este botón")
        _save_env({"QSTS_TELEGRAM_CHAT_ID": str(chat["id"])})
        ctx.settings.telegram_chat_id = str(chat["id"])
        return chat

    @app.post("/api/telegram/test")
    def tg_test():
        tg = _telegram()
        if tg is None:
            raise HTTPException(400, "Telegram no está configurado")
        try:
            tg.send("✅ <b>QSTS conectado.</b> Aquí recibirás cada operación de tus bots en paper trading.")
        except TelegramError as e:
            raise HTTPException(400, str(e))
        return {"sent": True}

    # ------------------------------------------------------------------ frontend
    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/")
        def index():
            return FileResponse(STATIC / "index.html")

    return app
