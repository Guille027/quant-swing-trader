"""Strategy Lab backend: the full validation pipeline.

Hypothesis -> definition -> causality check -> in-sample backtest -> parameter robustness ->
walk-forward -> Monte Carlo -> cost sensitivity -> baselines -> ablation -> overfitting risk ->
OOS (vault, once) -> multi-objective score -> REJECTED | CANDIDATE.

Only research-period data (before the OOS boundary) is used for every step except the single
vault evaluation, which happens last and cannot influence any earlier choice.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from qsts.backtest.benchmarks import momentum_baseline, trend_baseline
from qsts.backtest.engine import BacktestConfig
from qsts.backtest.integrity import LookAheadError, check_strategy_causality
from qsts.backtest.metrics import periodic_returns, regime_metrics
from qsts.research.experiments import ExperimentTracker
from qsts.research.scoring import OverfitConfig, ScoreConfig, overfitting_risk, strategy_score
from qsts.research.validation import (Objective, OOSVault, cost_sensitivity, make_folds, monte_carlo_trades,
                                      parameter_robustness, run_window, walk_forward)
from qsts.strategy.definition import StrategyDefinition


def ablation(sd: StrategyDefinition, data, cfg, start, end, objective: Objective = Objective(), regime=None) -> dict:
    """FULL vs each entry/exit rule removed. A rule whose removal does not hurt adds no value."""
    _, full = run_window(sd, data, cfg, start, end, regime)
    out = {"FULL": objective(full)}
    for attr in ("entry_long", "entry_short", "exit_long", "exit_short"):
        rules = getattr(sd, attr)
        if len(rules) < (2 if attr.startswith("entry") else 1):
            continue  # removing the only entry rule leaves no strategy
        for i in range(len(rules)):
            variant = replace(sd, **{attr: rules[:i] + rules[i + 1:]})
            _, mt = run_window(variant, data, cfg, start, end, regime)
            c = rules[i]
            out[f"-{attr}[{i}] {c.left.feature or c.left.value} {c.op} {c.right.feature or c.right.value}"] = objective(mt)
    return out


@dataclass
class PipelineConfig:
    wf_train: int = 504
    wf_validate: int = 126
    wf_test: int = 126
    embargo: int = 5
    mc_sims: int = 2000
    seed: int = 0
    objective: Objective = field(default_factory=Objective)
    overfit: OverfitConfig = field(default_factory=OverfitConfig)
    score: ScoreConfig = field(default_factory=ScoreConfig)


def run_pipeline(sd: StrategyDefinition, data: dict[str, pd.DataFrame], vault: OOSVault, tracker: ExperimentTracker,
                 space: dict[str, list], bt_cfg: BacktestConfig = BacktestConfig(), pc: PipelineConfig = PipelineConfig(),
                 regime: pd.Series | None = None, evaluate_oos: bool = True) -> dict:
    report: dict = {"strategy": sd.name, "version_id": sd.version_id, "steps": {}}
    research = vault.research_view(data)
    longest = max(research.values(), key=len)

    # 1. causality ---------------------------------------------------------------
    try:
        check_strategy_causality(sd, longest)
        report["steps"]["causality"] = "PASS"
    except LookAheadError as e:
        report["steps"]["causality"] = f"FAIL: {e}"
        report["decision"] = "REJECTED"
        return report

    start = max(df.index.min() for df in research.values())
    end = max(df.index.max() for df in research.values())

    # 2. in-sample backtest (tracked) -----------------------------------------------
    rec = tracker.run_backtest(sd, research, bt_cfg, start=start, end=end, seed=pc.seed, regime=regime)
    report["experiment_id"] = rec.id
    is_m = rec.metrics
    report["steps"]["in_sample"] = is_m

    # 3. robustness ------------------------------------------------------------------
    rob = parameter_robustness(sd, research, bt_cfg, start, end, objective=pc.objective, regime=regime) if sd.params else None
    report["steps"]["robustness"] = None if rob is None else {
        "stability": rob.stability, "peak_sharpness": rob.peak_sharpness, "passed": rob.passed}

    # 4. walk-forward ----------------------------------------------------------------
    idx = pd.DatetimeIndex(sorted(set().union(*[d.index for d in research.values()])))
    folds = make_folds(idx, pc.wf_train, pc.wf_validate, pc.wf_test, pc.embargo)
    wf = walk_forward(sd, research, space, folds, bt_cfg, pc.objective, regime=regime) if folds and space else None
    report["steps"]["walk_forward"] = wf.summary if wf else "SKIPPED (insufficient data or no parameter space)"
    n_trials = max(len(folds), 1) * max(int(np.prod([len(v) for v in space.values()])) if space else 1, 1)

    # 5. Monte Carlo + costs ---------------------------------------------------------
    mc = monte_carlo_trades(rec.result.trades, bt_cfg.initial_capital, pc.mc_sims, pc.seed)
    report["steps"]["monte_carlo"] = mc
    report["steps"]["cost_sensitivity"] = cost_sensitivity(sd, research, bt_cfg, start, end, regime=regime)

    # 6. baselines & ablation --------------------------------------------------------
    base = {}
    for b in (momentum_baseline(), trend_baseline()):
        _, mb = run_window(b, research, bt_cfg, start, end)
        base[b.name] = mb.get("sharpe")
    report["steps"]["baselines"] = base
    report["steps"]["ablation"] = ablation(sd, research, bt_cfg, start, end, pc.objective, regime)

    # 7. overfitting risk ------------------------------------------------------------
    ovf = overfitting_risk(metrics=is_m, complexity=sd.complexity(), trades=rec.result.trades,
                           returns=rec.result.returns, n_trials=n_trials,
                           wf_efficiency=wf.summary.get("wf_efficiency") if wf else None,
                           param_stability=rob.stability if rob else None, cfg=pc.overfit)
    report["steps"]["overfitting"] = ovf

    # 8. OOS (single, logged access) -------------------------------------------------
    oos = None
    if evaluate_oos:
        oos_full = vault.evaluate(sd, data, bt_cfg, purpose=f"pipeline final evaluation exp={rec.id}", regime=regime)
        oos = {k: v for k, v in oos_full.items() if not k.startswith("_")}
        report["steps"]["oos"] = oos

    # 9. score -----------------------------------------------------------------------
    eq = rec.result.equity["equity"]
    sc = strategy_score(is_metrics=is_m, oos_metrics=oos, wf_summary=wf.summary if wf else None,
                        robustness_stability=rob.stability if rob else None, mc=mc if "total_return" in mc else None,
                        overfit=ovf, complexity=sd.complexity(), monthly_returns=periodic_returns(eq, "ME"),
                        regime_stats=regime_metrics(eq, regime) if regime is not None else None,
                        baseline_sharpes=base, cfg=pc.score)
    report["score"] = sc
    report["decision"] = sc["decision"]
    return report
