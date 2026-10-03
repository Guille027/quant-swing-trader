"""Overfitting Risk Score, Deflated Sharpe Ratio and multi-objective Strategy Score.

Every component is a documented, concrete statistic mapped to [0, 1]; the aggregate is a weighted
mean of the components that could be computed (missing ones are listed, never imputed).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

EULER_GAMMA = 0.5772156649


# ============================================================== Sharpe statistics
def probabilistic_sharpe(returns: pd.Series, sr_benchmark: float = 0.0) -> float:
    """PSR (Bailey & Lopez de Prado 2012): P(true per-period SR > benchmark) given sample length,
    skewness and kurtosis of returns. Per-period (not annualised) Sharpe."""
    r = pd.Series(returns).dropna()
    t = len(r)
    if t < 3 or r.std(ddof=1) == 0:
        return float("nan")
    sr = r.mean() / r.std(ddof=1)
    g3, g4 = skew(r), kurtosis(r, fisher=False)
    denom = np.sqrt(max(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2, 1e-12))
    return float(norm.cdf((sr - sr_benchmark) * np.sqrt(t - 1) / denom))


def expected_max_sharpe(n_trials: int, var_trial_sr: float) -> float:
    """Expected maximum per-period SR among n_trials unskilled strategies (False Strategy Theorem)."""
    if n_trials <= 1:
        return 0.0
    return float(np.sqrt(var_trial_sr) * ((1 - EULER_GAMMA) * norm.ppf(1 - 1 / n_trials)
                                          + EULER_GAMMA * norm.ppf(1 - 1 / (n_trials * np.e))))


def deflated_sharpe(returns: pd.Series, n_trials: int, var_trial_sr: float | None = None) -> float:
    """DSR (Bailey & Lopez de Prado 2014): PSR against the SR expected from the best of `n_trials`
    noise strategies. `var_trial_sr`: variance of per-period SR across the trials actually run;
    if unknown, the sampling variance 1/T of a zero-SR estimate is used (conservative lower bound)."""
    r = pd.Series(returns).dropna()
    if var_trial_sr is None:
        var_trial_sr = 1.0 / max(len(r), 1)
    return probabilistic_sharpe(r, expected_max_sharpe(n_trials, var_trial_sr))


# ============================================================== overfitting risk
@dataclass(frozen=True)
class OverfitConfig:
    trades_per_param_target: float = 30.0  # rule of thumb: >=30 independent trades per free parameter
    good_trade_count: int = 100  # below this, trade statistics are very noisy
    max_complexity: float = 15.0  # complexity score (params+rules+features) treated as maximal
    perfect_sharpe: float = 3.0  # annual Sharpe above this on daily swing data is suspicious
    perfect_profit_factor: float = 4.0
    perfect_win_rate: float = 0.8
    weights: dict = field(default_factory=lambda: {
        "trades_per_param": 1.0, "low_trade_count": 1.0, "deflated_sharpe": 1.5, "is_oos_degradation": 1.5,
        "parameter_sensitivity": 1.0, "profit_concentration": 1.0, "complexity": 0.5, "too_perfect": 1.0})


def profit_concentration(trades: pd.DataFrame, top_frac: float = 0.05) -> float:
    """Share of net profit produced by the best `top_frac` of trades (>1 means the rest lose)."""
    if trades is None or trades.empty or trades["pnl"].sum() <= 0:
        return float("nan")
    k = max(1, int(np.ceil(len(trades) * top_frac)))
    return float(trades["pnl"].nlargest(k).sum() / trades["pnl"].sum())


def overfitting_risk(*, metrics: dict, complexity: dict, trades: pd.DataFrame | None = None,
                     returns: pd.Series | None = None, n_trials: int = 1, var_trial_sr: float | None = None,
                     wf_efficiency: float | None = None, param_stability: float | None = None,
                     cfg: OverfitConfig = OverfitConfig()) -> dict:
    comp: dict[str, float] = {}
    nt = metrics.get("n_trades", 0) or 0
    npar = max(complexity.get("n_params", 0), 1)
    comp["trades_per_param"] = float(np.clip(1 - (nt / npar) / cfg.trades_per_param_target, 0, 1))
    comp["low_trade_count"] = float(np.clip(1 - nt / cfg.good_trade_count, 0, 1))
    if returns is not None and len(returns) > 30:
        dsr = deflated_sharpe(returns, n_trials, var_trial_sr)
        if np.isfinite(dsr):
            comp["deflated_sharpe"] = 1 - dsr
    if wf_efficiency is not None and np.isfinite(wf_efficiency):
        comp["is_oos_degradation"] = float(np.clip(1 - wf_efficiency, 0, 1))
    if param_stability is not None and np.isfinite(param_stability):
        comp["parameter_sensitivity"] = float(1 - param_stability)
    pc = profit_concentration(trades) if trades is not None else float("nan")
    if np.isfinite(pc):
        comp["profit_concentration"] = float(np.clip((pc - 0.5) / 0.5, 0, 1))
    comp["complexity"] = float(np.clip(complexity.get("score", 0) / cfg.max_complexity, 0, 1))
    sh, pf, wr = metrics.get("sharpe"), metrics.get("profit_factor"), metrics.get("win_rate")
    perfect = any([sh is not None and np.isfinite(sh) and sh > cfg.perfect_sharpe,
                   pf is not None and pf != "inf" and np.isfinite(float(pf)) and float(pf) > cfg.perfect_profit_factor,
                   pf == "inf" or (isinstance(pf, float) and np.isinf(pf) and nt > 0),
                   wr is not None and wr > cfg.perfect_win_rate])
    comp["too_perfect"] = 1.0 if perfect else 0.0
    w = cfg.weights
    score = sum(w[k] * v for k, v in comp.items()) / sum(w[k] for k in comp)
    missing = sorted(set(w) - set(comp))
    return {"score": float(score), "components": comp, "missing": missing,
            "level": "HIGH" if score >= 0.6 else "MEDIUM" if score >= 0.35 else "LOW"}


# ============================================================== strategy score
@dataclass(frozen=True)
class ScoreConfig:
    """Hard gates first (any failure => REJECTED regardless of score), then a weighted score.
    Gate thresholds are deliberately conservative defaults and fully configurable."""
    min_trades: int = 30
    max_overfit_risk: float = 0.6
    min_wf_efficiency: float = 0.3
    require_oos_positive: bool = True
    require_beats_baseline: bool = True
    max_drawdown_limit: float = -0.35
    weights: dict = field(default_factory=lambda: {
        "return": 1.0, "risk_adjusted": 2.0, "drawdown": 1.5, "consistency": 1.0, "robustness": 1.5,
        "oos": 2.0, "walk_forward": 1.5, "monte_carlo": 1.0, "complexity": 0.5, "trade_count": 0.5,
        "regime_stability": 1.0})


def _sig(x, scale):  # map a real metric to (0,1) smoothly; 0 -> 0.5
    return float(1 / (1 + np.exp(-x / scale))) if x is not None and np.isfinite(x) else None


def strategy_score(*, is_metrics: dict, oos_metrics: dict | None, wf_summary: dict | None,
                   robustness_stability: float | None, mc: dict | None, overfit: dict, complexity: dict,
                   monthly_returns: pd.Series | None = None, regime_stats: dict | None = None,
                   baseline_sharpes: dict[str, float] | None = None, cfg: ScoreConfig = ScoreConfig()) -> dict:
    gates: dict[str, bool] = {}
    gates["min_trades"] = (is_metrics.get("n_trades", 0) or 0) >= cfg.min_trades
    gates["overfit_risk"] = overfit["score"] <= cfg.max_overfit_risk
    gates["max_drawdown"] = (is_metrics.get("max_drawdown") or 0) >= cfg.max_drawdown_limit
    if wf_summary is not None:
        eff = wf_summary.get("wf_efficiency")
        gates["walk_forward"] = eff is not None and np.isfinite(eff) and eff >= cfg.min_wf_efficiency
    if cfg.require_oos_positive:
        gates["oos_positive"] = oos_metrics is not None and (oos_metrics.get("total_return") or 0) > 0
    if cfg.require_beats_baseline and baseline_sharpes:
        s = is_metrics.get("sharpe")
        gates["beats_baselines"] = s is not None and np.isfinite(s) and all(s > b for b in baseline_sharpes.values() if b is not None and np.isfinite(b))

    c: dict[str, float | None] = {
        "return": _sig(is_metrics.get("cagr"), 0.1),
        "risk_adjusted": _sig(is_metrics.get("sharpe"), 0.5),
        "drawdown": float(np.clip(1 + (is_metrics.get("max_drawdown") or 0) / abs(cfg.max_drawdown_limit), 0, 1)),
        "consistency": float((monthly_returns > 0).mean()) if monthly_returns is not None and len(monthly_returns) else None,
        "robustness": robustness_stability,
        "oos": _sig(oos_metrics.get("sharpe"), 0.5) if oos_metrics else None,
        "walk_forward": float(np.clip(wf_summary["wf_efficiency"], 0, 1)) if wf_summary and wf_summary.get("wf_efficiency") is not None and np.isfinite(wf_summary["wf_efficiency"]) else None,
        "monte_carlo": (1.0 if mc["total_return"]["p5"] > 0 else _sig(mc["total_return"]["p5"], 0.1)) if mc and "total_return" in mc else None,
        "complexity": float(1 - np.clip(complexity.get("score", 0) / 15, 0, 1)),
        "trade_count": float(np.clip((is_metrics.get("n_trades", 0) or 0) / 100, 0, 1)),
        "regime_stability": (float(np.mean([v["sharpe"] > 0 for v in regime_stats.values() if v.get("sharpe") is not None and np.isfinite(v["sharpe"])]))
                             if regime_stats else None),
    }
    avail = {k: v for k, v in c.items() if v is not None and np.isfinite(v)}
    w = cfg.weights
    raw = sum(w[k] * v for k, v in avail.items()) / sum(w[k] for k in avail) if avail else 0.0
    score = raw * (1 - overfit["score"])
    passed = all(gates.values())
    return {"score": float(score), "raw_score": float(raw), "components": c, "gates": gates,
            "missing": sorted(k for k in c if k not in avail),
            "decision": "CANDIDATE" if passed else "REJECTED",
            "failed_gates": sorted(k for k, v in gates.items() if not v)}
