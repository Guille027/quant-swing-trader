"""Run the baseline strategies through the full validation pipeline on stored data.

  python scripts/run_baselines.py [--oos-start 2023-01-01] [--benchmark SPY]

Uses whatever bars are in the configured database (ingest first with `qsts ingest`). The OOS vault
allows ONE evaluation per strategy version: a second run of the same version is refused by design.
Full reports are written to var/reports/.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from qsts.app.context import build_context
from qsts.backtest.benchmarks import buy_and_hold
from qsts.backtest.engine import BacktestConfig
from qsts.backtest.metrics import compute_metrics
from qsts.research.pipeline import run_pipeline
from qsts.research.validation import OOSVault
from qsts.strategy.definition import Condition, F, StopRule, StrategyDefinition, TakeProfitRule, V
from qsts.strategy.regime import classify


def trend_p() -> StrategyDefinition:
    """backtest.benchmarks.trend_baseline with the SMA length as a parameter (default 200 = same rules)."""
    return StrategyDefinition(
        name="baseline_trend", family="trend_following", hypothesis="Price above long-term average trends up",
        entry_long=(Condition(F("close"), ">", F("sma", n="$n")),),
        exit_long=(Condition(F("close"), "<", F("sma", n="$n")),),
        stop=StopRule("atr", 14, 4.0), take_profit=TakeProfitRule("none"), params={"n": 200.0})


def momentum_p() -> StrategyDefinition:
    """backtest.benchmarks.momentum_baseline with the ROC length as a parameter (default 126 = same rules)."""
    return StrategyDefinition(
        name="baseline_momentum", family="momentum", hypothesis="Positive 6-month momentum persists",
        entry_long=(Condition(F("roc", n="$n"), ">", V(0.0)),),
        exit_long=(Condition(F("roc", n="$n"), "<", V(0.0)),),
        stop=StopRule("atr", 14, 4.0), take_profit=TakeProfitRule("none"), params={"n": 126.0})


SPACES = {"baseline_trend": {"n": [100, 150, 200, 250]}, "baseline_momentum": {"n": [63, 126, 189, 252]}}
KEYS = ("cagr", "sharpe", "max_drawdown", "volatility", "n_trades", "win_rate", "profit_factor", "exposure",
        "total_costs")


def _bh_metrics(closes: dict[str, pd.Series], start, end, cfg: BacktestConfig) -> dict:
    """Equal-weight buy & hold (no rebalancing), entry costs paid; adjusted (total-return) closes."""
    curves = []
    for c in closes.values():
        c = c[(c.index >= start) & (c.index <= end)]
        curves.append(buy_and_hold(c, cfg)["equity"] / cfg.initial_capital)
    eq = pd.concat(curves, axis=1).dropna().mean(axis=1) * cfg.initial_capital
    return compute_metrics(pd.DataFrame({"equity": eq, "gross_exposure": 1.0}), pd.DataFrame(), cfg.bars_per_year)


def _short(m: dict | None) -> dict:
    return {k: (round(m[k], 4) if isinstance(m.get(k), float) else m.get(k)) for k in KEYS if m and k in m}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos-start", default="2023-01-01")
    ap.add_argument("--benchmark", default="SPY")
    args = ap.parse_args(argv)
    ctx = build_context()
    syms = [s for s in ctx.symbols() if s != args.benchmark]
    data = {s: ctx.research_frame(s) for s in syms}
    bench = ctx.research_frame(args.benchmark)
    regime = classify(bench)["trend"]
    oos = pd.Timestamp(args.oos_start, tz="UTC")
    cfg = BacktestConfig()
    vault = OOSVault(ctx.sf, oos)
    first = max(df.index.min() for df in data.values())
    last = min(df.index.max() for df in data.values())
    research_end = max(df.index[df.index < oos].max() for df in data.values())
    out = {"universe": syms, "benchmark": args.benchmark, "research": [str(first.date()), str(research_end.date())],
           "oos": [str(oos.date()), str(last.date())], "config": cfg.to_dict(), "strategies": {}}
    out["buy_and_hold"] = {
        f"{args.benchmark}_research": _short(_bh_metrics({args.benchmark: bench["close"]}, first, research_end, cfg)),
        f"{args.benchmark}_oos": _short(_bh_metrics({args.benchmark: bench["close"]}, oos, last, cfg)),
        "equal_weight_research": _short(_bh_metrics({s: d["close"] for s, d in data.items()}, first, research_end, cfg)),
        "equal_weight_oos": _short(_bh_metrics({s: d["close"] for s, d in data.items()}, oos, last, cfg)),
    }
    reports = Path(ctx.settings.state_dir) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    for sd in (trend_p(), momentum_p()):
        ctx.registry.register(sd.name, sd, origin="human")
        rep = run_pipeline(sd, data, vault, ctx.tracker, SPACES[sd.name], cfg, regime=regime)
        (reports / f"{sd.name}_{sd.version_id}.json").write_text(json.dumps(rep, indent=2, default=str))
        st = rep["steps"]
        out["strategies"][sd.name] = {
            "version_id": sd.version_id, "decision": rep["decision"], "experiment_id": rep.get("experiment_id"),
            "causality": st["causality"], "in_sample": _short(st.get("in_sample")), "oos": _short(st.get("oos")),
            "walk_forward": st.get("walk_forward"), "robustness": st.get("robustness"),
            "monte_carlo": {k: v for k, v in (st.get("monte_carlo") or {}).items() if k != "paths"},
            "cost_sensitivity": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}
                                 for r in st.get("cost_sensitivity", [])],
            "overfitting": st.get("overfitting"), "score": rep.get("score")}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
