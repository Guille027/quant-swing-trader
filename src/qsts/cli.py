"""Command line interface.

  qsts ingest  --provider yahoo|csv [--root DIR] --symbols AAPL,MSFT --start 2010-01-01 [--end ...]
  qsts serve   [--port 8765]            # backend + UI in the browser (127.0.0.1 only)
  qsts desktop                           # same, in a native window if pywebview is installed
  qsts scan    [--asof ISO] [--universe A,B]
  qsts research --strategy file.json --symbols A,B --oos-start 2022-01-01 --space '{"p": [1,2]}'
  qsts evolve  --symbols A,B --train 2012-01-01:2017-12-31 --validate 2018-01-15:2021-12-31
  qsts autoresearch [--cycles 1] [--no-ai]   # automatic search for the most consistent strategy
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from qsts.app.context import build_context
from qsts.data.bars import Timeframe
from qsts.data.quality import DataQualityError


def _provider(args):
    if args.provider == "csv":
        from qsts.data.providers.csv_provider import CSVProvider
        return CSVProvider(args.root)
    from qsts.data.providers.yfinance_provider import YFinanceProvider
    return YFinanceProvider()


def cmd_ingest(args, ctx):
    from qsts.app.datajobs import ingest_one
    prov = _provider(args)
    end = args.end or pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    ok = 0
    for sym in args.symbols.split(","):
        sym = sym.strip().upper().replace(".", "-")
        try:
            r = ingest_one(ctx.repo, prov, sym, args.start, end)
            print(f"{sym}: {r['bars']} bars stored" + (f" (warnings: {', '.join(r['warnings'])})" if r["warnings"] else ""))
            ok += 1
        except DataQualityError as e:
            print(f"{sym}: REJECTED - {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"{sym}: FAILED - {e!r}", file=sys.stderr)
    print(f"{ok} symbols ingested")
    print("NOTE: prices are stored RAW with corporate actions; research, charts and scans adjust them "
          "(qsts.data.adjust) using only actions known at the time.")


def _app(ctx):
    from qsts.api.server import create_app
    return create_app(ctx)


def cmd_serve(args, ctx):
    import uvicorn
    print(f"QSTS UI: http://127.0.0.1:{args.port}  (mode {ctx.modes.mode.name}, env {ctx.settings.env.value})")
    uvicorn.run(_app(ctx), host="127.0.0.1", port=args.port, log_level="warning")


def cmd_desktop(args, ctx):
    import threading
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(_app(ctx), host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    try:
        import webview
        webview.create_window("QSTS — Quant Swing Trading System", url, width=1400, height=900)
        webview.start()
    except ImportError:
        import time
        import webbrowser
        print(f"pywebview not installed; opening {url} in your browser (Ctrl+C to quit)")
        webbrowser.open(url)
        while True:
            time.sleep(3600)


def cmd_scan(args, ctx):
    from qsts.api.server import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app(ctx))
    params = {k: v for k, v in (("asof", args.asof), ("universe", args.universe)) if v}
    r = c.post("/api/scan", params=params)
    r.raise_for_status()
    print(c.get("/api/scan/text").text)


def _load_data(ctx, symbols):
    return {s: ctx.research_frame(s) for s in symbols}


def cmd_research(args, ctx):
    from qsts.research.pipeline import run_pipeline
    from qsts.research.validation import OOSVault
    from qsts.strategy.definition import definition_from_dict
    sd = definition_from_dict(json.loads(open(args.strategy).read()))
    data = _load_data(ctx, args.symbols.split(","))
    ctx.registry.register(args.strategy_id or sd.name, sd, origin="human")
    rep = run_pipeline(sd, data, OOSVault(ctx.sf, args.oos_start), ctx.tracker, json.loads(args.space or "{}"))
    print(json.dumps(rep, indent=2, default=str))
    print(f"\nDECISION: {rep['decision']}")


def cmd_evolve(args, ctx):
    from qsts.research.evolution import EvolutionConfig, EvolutionEngine
    tr = [pd.Timestamp(x, tz="UTC") for x in args.train.split(":")]
    va = [pd.Timestamp(x, tz="UTC") for x in args.validate.split(":")]
    data = _load_data(ctx, args.symbols.split(","))
    res = EvolutionEngine(data, tuple(tr), tuple(va), cfg=EvolutionConfig(population=args.population,
                                                                         generations=args.generations, seed=args.seed)).run()
    for ind in res["best"]:
        print(f"fitness={ind.fitness:.3f} train={ind.train:.3f} val={ind.val:.3f} {ind.sd.version_id}")
        print(json.dumps(ind.sd.to_dict(), default=str))
    print(f"trials evaluated: {res['n_trials']} — {res['note']}")


def cmd_autoresearch(args, ctx):
    from qsts.research.autoresearch import AutoResearchConfig
    cfg = AutoResearchConfig(oos_start=ctx.settings.oos_start, use_ai=not args.no_ai, population=args.population,
                             generations=args.generations)
    r = ctx.autoresearcher(cfg, log=print)
    if cfg.use_ai and r.ai is None:
        print("IA no configurada (QSTS_GEMINI_API_KEY vacío): solo búsqueda evolutiva")
    try:
        r.run(args.cycles)
    except KeyboardInterrupt:
        print("Interrumpido")
    lb = r.leaderboard(10)
    print(f"\nEstrategias probadas en total: {lb['n_trials']} · tests finales usados: {lb['final_tests_used']}")
    for i, row in enumerate(lb["rows"], 1):
        dsr = f"{row['dsr']:.0%}" if row["dsr"] is not None else "—"
        print(f"{i:2d}. consistencia {row['consistency']:+.3f} · Sharpe {row['sharpe'] or 0:.2f} · "
              f"DSR {dsr} · {row['status']} · [{row['origin']}] {row['rules']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="qsts")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest")
    s.add_argument("--provider", choices=["yahoo", "csv"], default="yahoo")
    s.add_argument("--root", default="data")
    s.add_argument("--symbols", required=True)
    s.add_argument("--start", default="2005-01-01")
    s.add_argument("--end")
    for name in ("serve", "desktop"):
        s = sub.add_parser(name)
        s.add_argument("--port", type=int, default=8765)
    s = sub.add_parser("scan")
    s.add_argument("--asof")
    s.add_argument("--universe")
    s = sub.add_parser("research")
    s.add_argument("--strategy", required=True)
    s.add_argument("--strategy-id")
    s.add_argument("--symbols", required=True)
    s.add_argument("--oos-start", required=True)
    s.add_argument("--space")
    s = sub.add_parser("evolve")
    s.add_argument("--symbols", required=True)
    s.add_argument("--train", required=True)
    s.add_argument("--validate", required=True)
    s.add_argument("--population", type=int, default=30)
    s.add_argument("--generations", type=int, default=10)
    s.add_argument("--seed", type=int, default=0)
    s = sub.add_parser("autoresearch")
    s.add_argument("--cycles", type=int, default=1, help="0 = until Ctrl+C")
    s.add_argument("--no-ai", action="store_true")
    s.add_argument("--population", type=int, default=20)
    s.add_argument("--generations", type=int, default=4)
    args = p.parse_args(argv)
    ctx = build_context()
    {"ingest": cmd_ingest, "serve": cmd_serve, "desktop": cmd_desktop, "scan": cmd_scan,
     "research": cmd_research, "evolve": cmd_evolve, "autoresearch": cmd_autoresearch}[args.cmd](args, ctx)


if __name__ == "__main__":
    main()
