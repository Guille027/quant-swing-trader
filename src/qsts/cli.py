"""Command line interface.

  qsts ingest   --provider yahoo|csv [--root DIR] --symbols AAPL,MSFT --start 2010-01-01 [--end ...]
  qsts serve    [--port 8765]            # backend + UI in the browser (127.0.0.1 only)
  qsts desktop                            # same, in a native window if pywebview is installed
  qsts shortcut                           # Windows: desktop/Start-menu shortcut that opens the app
  qsts backtest --strategy connors_rsi2 --symbol SPY   # one backtest in the terminal
  qsts strategies                         # the strategies in the library
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from qsts.app.context import build_context
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


def cmd_serve(args, ctx):
    import uvicorn
    from qsts.api.server import create_app
    print(f"QSTS UI: http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(ctx), host="127.0.0.1", port=args.port, log_level="warning")


def cmd_desktop(args, ctx):
    from qsts.app.launcher import main as launch
    launch(args.port)


def cmd_shortcut(args, ctx):
    from qsts.app.shortcut import main as make
    make()


def cmd_strategies(args, ctx):
    from qsts.lab.strategy import load_all
    for key, st in sorted(load_all().items()):
        print(f"{key:24s} {st.name}  ({', '.join(st.default_symbols)})")


def cmd_backtest(args, ctx):
    from qsts.lab import metrics
    from qsts.lab.backtest import run_backtest
    from qsts.lab.strategy import get
    res = run_backtest(get(args.strategy), ctx.research_frame(args.symbol.upper()))
    sm = metrics.summary(res.equity, res.trades, res.config.initial_capital, res.position)
    for k in ("first", "last", "net_profit_pct", "n_trades", "win_rate", "profit_factor", "max_drawdown", "sharpe"):
        print(f"{k:16s} {sm[k]}")


def main(argv=None):
    p = argparse.ArgumentParser("qsts")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest")
    s.add_argument("--provider", default="yahoo", choices=["yahoo", "csv"])
    s.add_argument("--root", default="data")
    s.add_argument("--symbols", required=True)
    s.add_argument("--start", default="2010-01-01")
    s.add_argument("--end")
    for name in ("serve", "desktop"):
        s = sub.add_parser(name)
        s.add_argument("--port", type=int, default=8765)
    sub.add_parser("shortcut", help="crea el acceso directo QSTS en el escritorio (Windows)")
    sub.add_parser("strategies")
    s = sub.add_parser("backtest")
    s.add_argument("--strategy", required=True)
    s.add_argument("--symbol", required=True)
    args = p.parse_args(argv)
    ctx = build_context()
    {"ingest": cmd_ingest, "serve": cmd_serve, "desktop": cmd_desktop, "shortcut": cmd_shortcut,
     "strategies": cmd_strategies, "backtest": cmd_backtest}[args.cmd](args, ctx)


if __name__ == "__main__":
    main()
