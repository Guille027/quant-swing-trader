"""Local backend API for the desktop UI (FastAPI). Binds to 127.0.0.1 only.

The UI is a thin client: every number it shows comes from these endpoints, which call the same
engines used everywhere else. Nothing is computed or invented in the frontend.
"""
from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, SecretStr
from sqlalchemy import select

from qsts.app.context import AppContext
from qsts.app.daily import DailyReporter
from qsts.app.datajobs import FX_SERIES, DataJobRunner, sample_symbols
from qsts.app import sync
from qsts.app.envfile import set_env_values
from qsts.app.scanner import MarketScanner, StrategySlot, render_report
from qsts.backtest.engine import BacktestConfig, CostModel
from qsts.core.modes import ModeTransitionError, SystemMode
from qsts.data.bars import Timeframe
from qsts.data.adjust import adjust
from qsts.data.quality import DataQualityError, validate_and_clean
from qsts.data.universe import UniverseList, fetch_sp500
from qsts.db import models as m
from qsts.execution.paper import PaperError, PaperTrading
from qsts.features.registry import REGISTRY, FeatureSet, FeatureSpec
from qsts.notify.telegram import Telegram, TelegramError
from qsts.research.autoresearch import AutoResearchConfig, AutoResearchRunner
from qsts.research.validation import OOSAccessDenied
from qsts.risk.engine import PortfolioState
from qsts.strategy.definition import definition_from_dict
from qsts.strategy.lifecycle import LifecycleError

STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"


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


class KillSwitchBody(BaseModel):
    engage: bool
    reason: str = "manual"
    confirm: bool = False


class ModeBody(BaseModel):
    mode: str
    reason: str
    confirm: bool = False


class BacktestBody(BaseModel):
    definition: dict
    symbols: list[str]
    start: str | None = None
    end: str | None = None
    initial_capital: float = 10_000.0
    risk_per_trade: float = 0.01
    spread_bps: float = 5.0
    slippage_bps: float = 5.0
    strategy_id: str | None = None


class AutoResearchBody(BaseModel):
    ignore_sync: bool = False  # start even though a newer copy from another computer waits to be loaded
    use_ai: bool = True
    avoid_earnings: bool = True
    max_cycles: int = 0  # 0 = until stopped
    population: int = 20
    generations: int = 4


class IngestBody(BaseModel):
    mode: str  # sp500 | symbols | update | earnings
    symbols: list[str] = []
    sample: int | None = None  # sp500: random sample size (None = all)
    start: str = "2010-01-01"


class PaperStartBody(BaseModel):
    strategy_id: str
    capital: float = 10_000.0
    currency: str = "USD"


class TelegramTokenBody(BaseModel):
    token: str


class KeyBody(BaseModel):
    key: str


class SyncDirBody(BaseModel):
    dir: str


class SyncSaveBody(BaseModel):
    force: bool = False


class SyncLoadBody(BaseModel):
    force: bool = False  # load even though the copy has less data than this computer
    path: str | None = None  # load_file: a copy downloaded by hand (default: newest in Downloads)


class PaperStopBody(BaseModel):
    reason: str = "detenida por el usuario"


class ApprovalBody(BaseModel):
    approve: bool
    reason: str = ""


def portfolio_state(ctx: AppContext) -> PortfolioState:
    try:
        acc = ctx.execution.broker.account()
        eq, cash = acc.equity, acc.cash
    except Exception:  # noqa: BLE001
        eq = cash = float("nan")
    peak = ctx.extra.setdefault("peak_equity", eq)
    if eq > peak:
        ctx.extra["peak_equity"] = peak = eq
    return PortfolioState(eq, cash, peak, ctx.extra.get("day_start", eq), ctx.extra.get("week_start", eq))


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        if ctx.extra.get("daily_reporter_autostart", True):
            _reporter().start()  # evening Telegram message of the paper-trading session
        yield
        rep = ctx.extra.get("daily_reporter")
        if rep is not None:
            rep.stop()

    app = FastAPI(title="QSTS", docs_url="/api/docs", lifespan=lifespan)
    if "code_version" not in ctx.extra:
        from qsts.research.experiments import code_version
        ctx.extra["code_version"] = code_version()

    @app.middleware("http")
    async def _no_stale_ui(request, call_next):
        # after an update the window must load the new screens, never a cached copy
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/shutdown")
    def shutdown():
        stop = ctx.extra.get("shutdown")
        if stop is None:
            raise HTTPException(409, "este servidor no admite apagado remoto")
        stop()
        return {"stopping": True}

    # ------------------------------------------------------------------ system
    @app.get("/api/status")
    def status():
        try:
            acc = ctx.execution.broker.account()
            broker = {"name": ctx.execution.broker.name, "environment": ctx.execution.broker.environment,
                      "connected": ctx.execution.connected, "cash": acc.cash, "equity": acc.equity,
                      "positions": ctx.execution.broker.positions()}
        except Exception as e:  # noqa: BLE001
            broker = {"name": ctx.execution.broker.name, "connected": False, "error": repr(e)}
        return _j({"environment": ctx.settings.env.value, "mode": ctx.modes.mode.name,
                   "code_version": ctx.extra.get("code_version"),
                   "live_enabled_by_config": ctx.settings.live_allowed_by_config,
                   "kill_switch": {"engaged": ctx.kill_switch.is_engaged(), "info": ctx.kill_switch.info()},
                   "broker": broker, "needs_reconcile": ctx.execution.needs_reconcile,
                   "symbols_with_data": len(ctx.symbols()), "strategies": ctx.strategy_counts(),
                   "ai": "configured" if ctx.settings.gemini_api_key else "not configured",
                   "pending_approvals": len(ctx.execution.pending)})

    @app.post("/api/kill-switch")
    def kill_switch(body: KillSwitchBody):
        if body.engage:
            ctx.execution.stop_all_trading(body.reason)
        else:
            try:
                ctx.kill_switch.release(confirmed_by_user=body.confirm)
            except PermissionError as e:
                raise HTTPException(400, str(e))
        return {"engaged": ctx.kill_switch.is_engaged()}

    @app.post("/api/mode")
    def set_mode(body: ModeBody):
        try:
            target = SystemMode[body.mode]
            ctx.modes.transition(target, reason=body.reason, confirmed_by_user=body.confirm)
        except (KeyError, ModeTransitionError) as e:
            raise HTTPException(400, str(e))
        return {"mode": ctx.modes.mode.name}

    # ------------------------------------------------------------------ market data / charts
    @app.get("/api/symbols")
    def symbols():
        return ctx.symbols()

    @app.get("/api/features")
    def features():
        return {k: {"category": d.category, "defaults": d.defaults, "version": d.version}
                for k, d in sorted(REGISTRY.items()) if not k.startswith("_")}

    @app.get("/api/chart/{symbol}")
    def chart(symbol: str, tf: str = "1d", indicators: str = "", asof: str | None = None):
        try:
            raw = ctx.load_bars(symbol, Timeframe(tf))
        except KeyError:
            raise HTTPException(404, f"no data for {symbol}")
        if raw.empty:
            raise HTTPException(404, f"no data for {symbol}")
        try:
            vb = validate_and_clean(raw[["open", "high", "low", "close", "volume"]], symbol, Timeframe(tf))
        except DataQualityError as e:
            raise HTTPException(422, str(e))
        df = vb.df
        acts = ctx.repo.load_corporate_actions(symbol)
        if asof:  # reconstruct what the system could see at `asof` (bars AND corporate actions)
            t = pd.Timestamp(asof)
            t = t.tz_localize("UTC") if t.tz is None else t
            df = df[df["available_at"] <= t]
            acts = acts[acts["ex_date"] <= t]
        df = adjust(df, acts, "total")  # split/dividend adjusted; raw prices are what is stored
        specs = []
        for tok in filter(None, indicators.split(",")):
            name, _, arg = tok.partition(":")
            if name not in REGISTRY:
                raise HTTPException(400, f"unknown indicator {name}")
            params = {"n": int(arg)} if arg else {}
            specs.append(FeatureSpec(name, params))
        feats = FeatureSet(specs).compute(df) if specs else pd.DataFrame(index=df.index)
        t = [int(x.timestamp()) for x in df.index]
        return _j({"symbol": symbol, "timeframe": tf, "data_version": vb.version,
                   "quality": [{"code": i.code, "severity": i.severity.value, "message": i.message} for i in vb.report.issues],
                   "candles": [{"time": ti, "open": o, "high": h, "low": l, "close": c}
                               for ti, o, h, l, c in zip(t, df["open"], df["high"], df["low"], df["close"])],
                   "volume": [{"time": ti, "value": v} for ti, v in zip(t, df["volume"])],
                   "indicators": {col: [{"time": ti, "value": v} for ti, v in zip(t, feats[col]) if pd.notna(v)]
                                  for col in feats.columns}})

    # ------------------------------------------------------------------ strategies / experiments
    @app.get("/api/strategies")
    def strategies():
        with ctx.sf() as s:
            rows = s.scalars(select(m.Strategy).order_by(m.Strategy.created_at)).all()
            out = []
            for r in rows:
                vers = s.scalars(select(m.StrategyVersion).where(m.StrategyVersion.strategy_id == r.id)
                                 .order_by(m.StrategyVersion.version)).all()
                out.append({"id": r.id, "name": r.name, "family": r.family, "status": r.status, "origin": r.origin,
                            "versions": [{"id": v.id, "version": v.version, "definition": v.definition} for v in vers]})
        return _j(out)

    @app.get("/api/strategies/{sid}/history")
    def strategy_history(sid: str):
        return ctx.registry.history(sid)

    @app.get("/api/experiments")
    def experiments(limit: int = 100):
        with ctx.sf() as s:
            rows = s.scalars(select(m.Experiment).order_by(m.Experiment.created_at.desc()).limit(limit)).all()
            return _j([{"id": e.id, "kind": e.kind, "strategy_version_id": e.strategy_version_id,
                        "dataset_version_id": e.dataset_version_id, "seed": e.seed, "code_version": e.code_version,
                        "created_at": str(e.created_at), "metrics": e.metrics,
                        "period": [e.config.get("start"), e.config.get("end")], "symbols": e.config.get("symbols")}
                       for e in rows])

    @app.get("/api/experiments/{eid}")
    def experiment(eid: str):
        with ctx.sf() as s:
            e = s.get(m.Experiment, eid)
            if e is None:
                raise HTTPException(404)
            bt = s.scalars(select(m.Backtest).where(m.Backtest.experiment_id == eid)).first()
            return _j({"id": e.id, "config": e.config, "metrics": e.metrics, "seed": e.seed,
                       "code_version": e.code_version, "strategy_version_id": e.strategy_version_id,
                       "trades": bt.trades if bt else [], "equity": bt.equity_curve if bt else []})

    @app.post("/api/experiments/{eid}/reproduce")
    def reproduce(eid: str):
        e = ctx.tracker.get(eid)
        data = {s: ctx.research_frame(s) for s in e.config["symbols"]}
        return _j(ctx.tracker.reproduce(eid, data))

    @app.post("/api/lab/backtest")
    def lab_backtest(body: BacktestBody):
        try:
            sd = definition_from_dict(body.definition)
            sd.validate()
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"invalid strategy: {e}")
        data = {}
        for sym in body.symbols:
            try:
                data[sym] = ctx.research_frame(sym)
            except (KeyError, DataQualityError) as e:
                raise HTTPException(422, f"{sym}: {e}")
        cfg = BacktestConfig(initial_capital=body.initial_capital, risk_per_trade=body.risk_per_trade,
                             costs=CostModel(spread_bps=body.spread_bps, slippage_bps=body.slippage_bps))
        start = pd.Timestamp(body.start, tz="UTC") if body.start else None
        end = pd.Timestamp(body.end, tz="UTC") if body.end else None
        rec = ctx.tracker.run_backtest(sd, data, cfg, start=start, end=end, strategy_id=body.strategy_id)
        eq = rec.result.equity["equity"]
        return _j({"experiment_id": rec.id, "strategy_version_id": sd.version_id, "metrics": rec.metrics,
                   "complexity": sd.complexity(),
                   "equity": [{"time": int(t.timestamp()), "value": v} for t, v in eq.items()],
                   "trades": rec.result.trades.astype(str).to_dict("records")[:500]})

    # ------------------------------------------------------------------ data manager ("Datos")
    def _invalidate_research_views():
        ctx.extra.pop("autoresearch_view", None)
        r = ctx.extra.get("autoresearch")
        if r is not None and not r.state.running:
            r.researcher = None

    def _yahoo():
        from qsts.data.providers.yfinance_provider import YFinanceProvider
        return YFinanceProvider()

    def _data_runner() -> DataJobRunner:
        r = ctx.extra.get("datajob")
        if r is None:
            r = ctx.extra["datajob"] = DataJobRunner(ctx.repo, ctx.extra.get("data_provider_factory") or _yahoo,
                                                     on_finish=_invalidate_research_views)
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
        joined = ctx.repo.membership_starts("SP500")
        for r in rows:
            r["sp500_since"] = str(joined[r["symbol"]]) if r["symbol"] in joined else None
        earn = ctx.repo.earnings_summary()
        for r in rows:
            r["earnings"] = earn.get(r["symbol"])
        return _j({"count": len(rows), "symbols": rows, "benchmark": ctx.settings.benchmark,
                   "earnings": {"symbols": len(earn), "events": sum(v["events"] for v in earn.values()),
                                "first": min((v["first"] for v in earn.values()), default=None)},
                   "earnings_rule": {"blackout_days": ctx.settings.earnings_blackout_days,
                                     "exit_before": ctx.settings.exit_before_earnings},
                   "has_benchmark": any(r["symbol"] == ctx.settings.benchmark for r in rows),
                   "first": min((r["first"] for r in rows), default=None), "last": max((r["last"] for r in rows), default=None)})

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
        bars = True
        if body.mode == "update":
            if not have:
                raise HTTPException(400, "no hay datos que actualizar")
            syms, incremental = have, True
        elif body.mode == "earnings":
            if not have:
                raise HTTPException(400, "primero descarga precios")
            syms, incremental, bars = have, False, False
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
            wanted = set(syms) | set(have)  # also fill name/sector of members you already had
            fields = {r.symbol: {"name": r.name, "sector": r.sector} for r in mem.itertuples() if r.symbol in wanted}
            members = ("SP500", [(r.symbol, r.date_added.date(), None) for r in mem.itertuples()
                                 if pd.notna(r.date_added)], ul.source)
            incremental = False
        else:
            raise HTTPException(400, "modo desconocido")
        if bars and bench not in syms and bench not in have:
            syms = [bench, *syms]
        started = _data_runner().start(body.mode, syms, body.start, incremental=incremental, asset_fields=fields,
                                       memberships=members, bars=bars)
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

    # ------------------------------------------------------------------ paper trading ("Simulación")
    def _paper() -> PaperTrading:
        p = ctx.extra.get("paper")
        if p is None:
            p = ctx.extra["paper"] = PaperTrading(ctx.sf, ctx.research_frame, ctx.settings.benchmark,
                                                  fx=lambda: ctx.repo.fx_series(FX_SERIES))
        return p

    @app.get("/api/paper")
    def paper_view():
        return _j(_paper().view())

    @app.get("/api/paper/summary")
    def paper_summary():
        return _j(_paper().summary())

    @app.get("/api/paper/journal")
    def paper_journal():
        return _j(_paper().journal())

    @app.post("/api/paper/start")
    def paper_start(body: PaperStartBody):
        try:
            return {"session_id": _paper().start(body.strategy_id, body.capital, currency=body.currency)}
        except (PaperError, LifecycleError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/paper/stop")
    def paper_stop(body: PaperStopBody):
        try:
            _paper().stop(body.reason)
        except (PaperError, LifecycleError) as e:
            raise HTTPException(400, str(e))
        return {"stopped": True}

    # ------------------------------------------------------------------ Telegram messages of the simulation
    def _tg_client(token: str | None = None, with_chat: bool = True):
        tok = token or (ctx.settings.telegram_bot_token.get_secret_value() if ctx.settings.telegram_bot_token else None)
        if not tok:
            return None
        chat = ctx.settings.telegram_chat_id if with_chat else None
        return (ctx.extra.get("telegram_factory") or Telegram)(tok, chat)

    def _telegram():
        return _tg_client() if ctx.settings.telegram_chat_id else None

    def _reporter() -> DailyReporter:
        r = ctx.extra.get("daily_reporter")
        if r is None:
            r = ctx.extra["daily_reporter"] = DailyReporter(
                ctx.sf, _paper, _telegram, _data_runner, ctx.repo.last_bar, benchmark=ctx.settings.benchmark,
                delay_min=ctx.settings.daily_report_delay_min, blocked=_sync_block_reason)
        return r

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

    @app.get("/api/sync")
    def sync_status():
        st = _sync_status()
        loaded = ctx.extra.get("sync_loaded")
        try:
            downloaded = sync.find_downloaded(ctx.extra.get("downloads_dir"))
        except OSError:
            downloaded = None
        return _j({**st, "loaded_at_start": loaded, "code_version": ctx.extra.get("code_version"),
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
            raise HTTPException(409, f"OJO: ese archivo tiene MENOS datos que este ordenador ({rs.get('strategies_tested') or 0} "
                                     f"estrategias y {rs.get('stocks') or 0} acciones, frente a "
                                     f"{ls.get('strategies_tested') or 0} y {ls.get('stocks') or 0} aquí). Si lo cargas, "
                                     "este ordenador perdería sus datos (quedaría una copia de seguridad).")
        restart = ctx.extra.get("restart_app")
        if restart is not None:
            restart()
        return _j({"staged": meta, "restarting": restart is not None})

    def _check_idle():
        r = ctx.extra.get("autoresearch")
        if r is not None and r.state.running:
            raise HTTPException(409, "detén primero la investigación")
        if _data_runner().state.running:
            raise HTTPException(409, "espera a que termine la descarga de datos")

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
            raise HTTPException(409, f"OJO: la copia de {(st.get('remote') or {}).get('machine')} tiene MENOS datos que este "
                                     f"ordenador ({rs.get('strategies_tested') or 0} estrategias y {rs.get('stocks') or 0} "
                                     f"acciones, frente a {ls.get('strategies_tested') or 0} y {ls.get('stocks') or 0} aquí). "
                                     "Si la cargas, este ordenador perdería sus datos (quedaría una copia de seguridad).")
        r = ctx.extra.get("autoresearch")
        if r is not None and r.state.running:
            raise HTTPException(409, "detén primero la investigación")
        if _data_runner().state.running:
            raise HTTPException(409, "espera a que termine la descarga de datos")
        try:
            meta = sync.stage_import(ctx.settings.state_dir)
        except (sync.SyncError, OSError) as e:
            raise HTTPException(409, str(e))
        restart = ctx.extra.get("restart_app")
        if restart is not None:
            restart()  # the launcher closes the window and opens QSTS again with the loaded data
        return _j({"staged": meta, "restarting": restart is not None})

    def _save_env(values: dict) -> None:
        set_env_values(values, ctx.extra.get("env_path", ".env"))

    @app.get("/api/telegram")
    def tg_status():
        rep = _reporter()
        with ctx.sf() as s:
            last = s.scalars(select(m.PaperNotification).order_by(m.PaperNotification.sent_at.desc()).limit(10)).all()
            last = [{"day": str(n.day), "kind": n.kind, "ok": n.ok, "error": n.error, "sent_at": str(n.sent_at)[:16]}
                    for n in last]
        return _j({"token_set": ctx.settings.telegram_bot_token is not None, "chat_id": ctx.settings.telegram_chat_id,
                   "configured": _telegram() is not None, "state": rep.state, "log": list(rep.logs)[-12:],
                   "last": last, "delay_min": ctx.settings.daily_report_delay_min})

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

    @app.post("/api/settings/gemini")
    def set_gemini_key(body: KeyBody):
        """The AI key is a secret: saved only to this computer's .env (never copied to other computers)."""
        key = body.key.strip()
        if len(key) < 20 or any(ch.isspace() for ch in key):
            raise HTTPException(400, "eso no parece una clave de Gemini (cópiala entera de Google AI Studio)")
        _save_env({"QSTS_GEMINI_API_KEY": key})
        ctx.settings.gemini_api_key = SecretStr(key)
        _invalidate_research_views()  # the next search is built with the AI
        return {"saved": True}

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
            tg.send("✅ <b>QSTS conectado.</b> Aquí recibirás cada tarde el resumen de la simulación y los avisos de venta.")
        except TelegramError as e:
            raise HTTPException(400, str(e))
        return {"sent": True}

    @app.post("/api/telegram/report")
    def tg_report():
        try:
            return _reporter().send_report("manual")
        except TelegramError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------------ automatic research ("Investigación IA")
    def _runner() -> AutoResearchRunner:
        r = ctx.extra.get("autoresearch")
        if r is None:
            r = ctx.extra["autoresearch"] = AutoResearchRunner(
                lambda cfg, log, stop: ctx.autoresearcher(cfg, log=log, stop_event=stop))
        return r

    options_file = Path(ctx.settings.state_dir) / "autoresearch_options.json"

    def _last_options() -> dict:
        """Options of the last research started (the ranking shown after a restart must use the same rules)."""
        try:
            return json.loads(options_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _researcher():
        r = _runner().researcher
        if r is None:  # read-only view for the leaderboard / final test when no loop has run yet
            r = ctx.extra.get("autoresearch_view")
            if r is None:
                r = ctx.extra["autoresearch_view"] = ctx.autoresearcher(AutoResearchConfig(
                    oos_start=ctx.settings.oos_start, use_ai=False,
                    avoid_earnings=bool(_last_options().get("avoid_earnings", True))))
        return r

    @app.get("/api/autoresearch/status")
    def ar_status():
        return _j(_runner().status() | {"ai_available": bool(ctx.settings.gemini_api_key), "last_options": _last_options(),
                                        "earnings_symbols": len(ctx.repo.earnings_summary()),
                                        "oos_start": ctx.settings.oos_start})

    @app.post("/api/autoresearch/start")
    def ar_start(body: AutoResearchBody):
        if not ctx.symbols():
            raise HTTPException(400, "no hay datos: ejecuta primero `qsts ingest`")
        newer = _sync_newer()
        if newer and not body.ignore_sync:
            raise HTTPException(409, f"Hay datos más recientes de {newer} en la carpeta compartida: cárgalos antes en "
                                     "Inicio. Si investigas ahora y luego los cargas, perderás lo que hagas aquí.")
        cfg = AutoResearchConfig(oos_start=ctx.settings.oos_start, use_ai=body.use_ai, avoid_earnings=body.avoid_earnings,
                                 population=max(4, min(body.population, 100)), generations=max(1, min(body.generations, 50)))
        started = _runner().start(cfg, max(0, body.max_cycles))
        if started:
            try:
                options_file.parent.mkdir(parents=True, exist_ok=True)
                options_file.write_text(json.dumps({"use_ai": body.use_ai, "avoid_earnings": body.avoid_earnings}),
                                        encoding="utf-8")
            except OSError:
                pass
            ctx.extra.pop("autoresearch_view", None)
        return {"started": started}

    @app.post("/api/autoresearch/stop")
    def ar_stop():
        _runner().stop()
        return {"stopping": True}

    @app.get("/api/autoresearch/leaderboard")
    def ar_leaderboard(limit: int = 20, group: bool = False):
        try:
            return _j(_researcher().leaderboard(max(1, min(limit, 200)), group=group))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/autoresearch/{vid}/backtest")
    def ar_backtest(vid: str):
        try:
            return _j(_researcher().backtest_view(vid))
        except KeyError:
            raise HTTPException(404, "estrategia desconocida")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/autoresearch/{vid}/final-test")
    def ar_final_test(vid: str):
        try:
            return _j(_researcher().final_test(vid))
        except KeyError:
            raise HTTPException(404, "estrategia desconocida")
        except OOSAccessDenied as e:
            raise HTTPException(409, f"el test final ya se usó para esta versión: {e}")
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------------ scan / signals / approvals
    @app.post("/api/scan")
    def scan(asof: str | None = None, universe: str | None = None):
        syms = universe.split(",") if universe else [s for s in ctx.symbols() if s != "SPY"]
        with ctx.sf() as s:
            slots = []
            for st in s.scalars(select(m.Strategy)).all():
                v = s.scalars(select(m.StrategyVersion).where(m.StrategyVersion.strategy_id == st.id)
                              .order_by(m.StrategyVersion.version.desc())).first()
                if v:
                    slots.append(StrategySlot(st.id, definition_from_dict(v.definition), st.status))
        t = pd.Timestamp(asof) if asof else pd.Timestamp.now(tz="UTC")
        sc = MarketScanner(lambda sym: ctx.adjusted_bars(sym, asof=t), slots, ctx.risk)
        rep = sc.scan(syms, t, portfolio_state(ctx))
        ctx.last_scan = rep
        return _j({"asof": rep.asof, "regime": rep.regime, "assets_scanned": rep.assets_scanned,
                   "valid_assets": rep.valid_assets, "invalid": rep.invalid, "potential_setups": rep.potential_setups,
                   "final_signals": rep.final_signals, "signals": [s.__dict__ for s in rep.signals],
                   "no_trade": [s.__dict__ for s in rep.no_trade[:200]]})

    @app.get("/api/scan/text", response_class=PlainTextResponse)
    def scan_text():
        if ctx.last_scan is None:
            return "No scan yet. POST /api/scan first."
        rep = ctx.last_scan
        data_state = ("OK" if rep.valid_assets == rep.assets_scanned else
                      f"PARTIAL ({len(rep.invalid)} invalid)" if rep.valid_assets else
                      ("NO DATA" if not rep.assets_scanned else f"INVALID/STALE ({', '.join(sorted(set(rep.invalid.values())))[:60]})"))
        sysinfo = {"Data": data_state,
                   "AI": "OK" if ctx.settings.gemini_api_key else "NOT CONFIGURED",
                   "Broker": ctx.execution.broker.environment.upper(),
                   "Risk Engine": "OK", "Kill Switch": "ENGAGED" if ctx.kill_switch.is_engaged() else "READY"}
        return render_report(ctx.last_scan, portfolio_state(ctx), ctx.strategy_counts(), sysinfo)

    @app.get("/api/approvals")
    def approvals():
        return _j([{"key": k, "signal": s.__dict__, "qty": d.qty, "risk_amount": d.risk_amount, "tier": d.tier.value}
                   for k, (s, d) in ctx.execution.pending.items()])

    @app.post("/api/approvals/{key}")
    def decide(key: str, body: ApprovalBody):
        if key not in ctx.execution.pending:
            raise HTTPException(404)
        if body.approve:
            o = ctx.execution.approve(key, actor="user")
            return _j({"status": o.status, "reasons": o.reasons})
        ctx.execution.reject(key, actor="user", reason=body.reason)
        return {"status": "REJECTED"}

    @app.get("/api/journal")
    def journal(limit: int = 200):
        return _j(ctx.execution.journal[-limit:])

    @app.get("/api/notifications")
    def notifications(limit: int = 200):
        return _j([{"ts": n.ts, "event": n.event.value, "title": n.title, "body": n.body}
                   for n in ctx.log_channel.sent[-limit:]])

    # ------------------------------------------------------------------ frontend
    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/")
        def index():
            return FileResponse(STATIC / "index.html")

    return app
