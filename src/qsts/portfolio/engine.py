"""Portfolio, correlation, confidence calibration and strategy-decay engines.

All inputs are histories known at decision time (callers pass trailing windows).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform
from scipy.stats import binomtest, norm, t as student_t


# ============================================================== covariance
def shrunk_covariance(returns: pd.DataFrame, shrinkage: float | None = None) -> pd.DataFrame:
    """Shrink the sample covariance toward a constant-correlation target (Ledoit-Wolf style).
    With few observations the sample covariance is noisy; shrinkage stabilises allocation.
    If `shrinkage` is None it is set to n_assets / (n_assets + n_obs) (more assets/fewer obs -> more)."""
    r = returns.dropna()
    s = r.cov()
    sd = np.sqrt(np.diag(s))
    corr = r.corr().to_numpy()
    n = len(sd)
    avg = (corr.sum() - n) / (n * (n - 1)) if n > 1 else 0.0
    target = avg * np.outer(sd, sd)
    np.fill_diagonal(target, sd ** 2)
    k = shrinkage if shrinkage is not None else n / (n + len(r))
    return pd.DataFrame((1 - k) * s.to_numpy() + k * target, index=s.index, columns=s.columns)


# ============================================================== strategy allocation
@dataclass(frozen=True)
class AllocationConfig:
    method: str = "risk_parity"  # inverse_vol | risk_parity | min_variance
    max_weight: float = 0.4
    min_health: float = 0.5  # strategies below this get zero capital
    min_observations: int = 60


def allocate(strategy_returns: pd.DataFrame, health: dict[str, float] | None = None,
             cfg: AllocationConfig = AllocationConfig()) -> pd.Series:
    """Weights over strategies; the remainder (1 - sum) is CASH. Higher return does NOT mean more capital:
    allocation is driven by risk (vol/covariance) and health, never by past mean return."""
    r = strategy_returns.dropna(how="all")
    health = health or {}
    eligible = [c for c in r.columns if r[c].count() >= cfg.min_observations and health.get(c, 1.0) >= cfg.min_health
                and r[c].std() > 0]
    w = pd.Series(0.0, index=r.columns)
    if not eligible:
        return w
    cov = shrunk_covariance(r[eligible].dropna())
    vol = np.sqrt(np.diag(cov))
    if cfg.method == "inverse_vol":
        raw = 1 / vol
    elif cfg.method == "min_variance":
        inv = np.linalg.pinv(cov.to_numpy())
        raw = np.clip(inv @ np.ones(len(eligible)), 0, None)
        if raw.sum() == 0:
            raw = 1 / vol
    else:  # risk parity via fixed-point iterations (equal risk contribution, long-only)
        C = cov.to_numpy()
        x = 1 / vol
        x /= x.sum()
        for _ in range(500):
            mrc = C @ x
            x_new = 1 / np.maximum(mrc, 1e-18)
            x_new /= x_new.sum()
            if np.max(np.abs(x_new - x)) < 1e-10:
                break
            x = 0.5 * x + 0.5 * x_new
        raw = x
    raw = pd.Series(raw / raw.sum(), index=eligible)
    raw = raw * pd.Series({c: min(health.get(c, 1.0), 1.0) for c in eligible})
    # cap iteratively; capped excess goes to cash, never forced into other strategies
    raw = raw.clip(upper=cfg.max_weight)
    w.loc[eligible] = raw
    return w


# ============================================================== correlation & concentration
def correlation_clusters(returns: pd.DataFrame, threshold: float = 0.7) -> dict[str, int]:
    """Hierarchical clustering on 1 - corr; assets with corr >= threshold end up together."""
    c = returns.dropna().corr().clip(-1, 1)
    if len(c) < 2:
        return {k: 1 for k in c.columns}
    dist = squareform((1 - c).to_numpy(), checks=False)
    z = linkage(dist, method="average")
    labels = fcluster(z, t=1 - threshold, criterion="distance")
    return dict(zip(c.columns, map(int, labels)))


def effective_bets(weights: pd.Series, returns: pd.DataFrame) -> float:
    """Effective number of independent bets = exp(entropy) of risk contributions on principal components."""
    w = weights.reindex(returns.columns).fillna(0).to_numpy()
    if not w.any():
        return 0.0
    cov = shrunk_covariance(returns).to_numpy()
    vals, vecs = np.linalg.eigh(cov)
    exp_ = (vecs.T @ w) ** 2 * vals
    p = exp_ / exp_.sum()
    p = p[p > 0]
    return float(np.exp(-(p * np.log(p)).sum()))


def concentration_report(positions: dict[str, float], returns: pd.DataFrame, sectors: dict[str, str],
                         benchmark: pd.Series | None = None, threshold: float = 0.7) -> dict:
    """positions: symbol -> signed notional. Flags e.g. NVDA/AMD/AVGO/TSM/MU as ONE cluster of risk."""
    syms = [s for s in positions if s in returns.columns]
    gross = sum(abs(v) for v in positions.values()) or 1.0
    clusters = correlation_clusters(returns[syms], threshold) if len(syms) > 1 else {s: 1 for s in syms}
    by_cluster: dict[int, list[str]] = {}
    for s, k in clusters.items():
        by_cluster.setdefault(k, []).append(s)
    sector_exp: dict[str, float] = {}
    for s, v in positions.items():
        sector_exp[sectors.get(s, "UNKNOWN")] = sector_exp.get(sectors.get(s, "UNKNOWN"), 0) + abs(v) / gross
    betas = {}
    if benchmark is not None:
        b = benchmark.reindex(returns.index)
        for s in syms:
            pair = pd.concat([returns[s], b], axis=1).dropna()
            betas[s] = float(pair.cov().iloc[0, 1] / pair.iloc[:, 1].var()) if len(pair) > 20 else np.nan
    w = pd.Series({s: positions[s] / gross for s in syms})
    return {
        "clusters": [sorted(v) for v in by_cluster.values()],
        "largest_cluster_share": max((sum(abs(positions[s]) for s in v) / gross for v in by_cluster.values()), default=0.0),
        "sector_exposure": sector_exp,
        "effective_bets": effective_bets(w, returns[syms]) if syms else 0.0,
        "n_positions": len(positions),
        "portfolio_beta": float(sum(w[s] * betas[s] for s in syms if np.isfinite(betas.get(s, np.nan)))) if betas else None,
        "betas": betas,
    }


# ============================================================== confidence calibration
def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


class ConfidenceCalibrator:
    """Maps a raw signal score to an EMPIRICAL win probability from out-of-sample signal history.

    A bin only produces a confidence when it has >= min_samples outcomes; otherwise `predict` returns
    None and the system must not display or use a confidence number."""

    def __init__(self, n_bins: int = 5, min_samples: int = 30):
        self.n_bins, self.min_samples = n_bins, min_samples
        self.edges: np.ndarray | None = None
        self.table: pd.DataFrame | None = None

    def fit(self, scores: pd.Series, wins: pd.Series) -> "ConfidenceCalibrator":
        d = pd.DataFrame({"s": scores, "w": wins.astype(int)}).dropna()
        self.edges = np.unique(np.quantile(d["s"], np.linspace(0, 1, self.n_bins + 1)))
        b = np.clip(np.searchsorted(self.edges, d["s"], side="right") - 1, 0, len(self.edges) - 2)
        rows = []
        for i in range(len(self.edges) - 1):
            g = d["w"][b == i]
            n, k = len(g), int(g.sum())
            lo, hi = wilson_interval(k, n)
            rows.append({"bin": i, "lo_edge": self.edges[i], "hi_edge": self.edges[i + 1], "n": n,
                         "win_rate": k / n if n else np.nan, "ci_low": lo, "ci_high": hi,
                         "usable": n >= self.min_samples})
        self.table = pd.DataFrame(rows)
        # Monotonicity check: if win rate does not increase with score, the score carries no ranking info.
        u = self.table[self.table["usable"]]
        self.monotonic = bool(len(u) >= 2 and u["win_rate"].is_monotonic_increasing)
        p = self.table.set_index("bin")["win_rate"].reindex(b).to_numpy()
        self.brier = float(np.nanmean((p - d["w"].to_numpy()) ** 2))
        return self

    def predict(self, score: float) -> float | None:
        if self.table is None:
            return None
        i = int(np.clip(np.searchsorted(self.edges, score, side="right") - 1, 0, len(self.edges) - 2))
        row = self.table.iloc[i]
        return float(row["win_rate"]) if row["usable"] else None


# ============================================================== strategy decay
@dataclass(frozen=True)
class DecayConfig:
    alpha: float = 0.05  # significance for "observed worse than expected"
    min_trades: int = 20  # below this no statistical verdict is possible
    dd_breach_multiple: float = 1.0  # observed DD beyond MC p5 drawdown * this -> breach
    disable_alpha: float = 0.01  # stronger evidence -> recommend DISABLED
    frequency_alpha: float = 0.01


def decay_check(observed: pd.DataFrame, expected: dict, cfg: DecayConfig = DecayConfig(),
                observed_days: float | None = None) -> dict:
    """observed: live/paper trades (pnl, r_multiple, slippage_bps optional).
    expected: {win_rate, expectancy_r, mc_p5_drawdown, trades_per_day, slippage_bps}."""
    n = len(observed)
    out: dict = {"n_trades": n, "tests": {}}
    if n < cfg.min_trades:
        out |= {"health": 1.0, "recommendation": "KEEP", "reason": f"only {n} trades (<{cfg.min_trades})"}
        return out
    p_values = {}
    wins = int((observed["pnl"] > 0).sum())
    p_values["win_rate"] = binomtest(wins, n, expected["win_rate"], alternative="less").pvalue
    r = observed["r_multiple"].dropna()
    if len(r) > 2 and r.std(ddof=1) > 0:
        tstat = (r.mean() - expected["expectancy_r"]) / (r.std(ddof=1) / np.sqrt(len(r)))
        p_values["expectancy"] = float(student_t.cdf(tstat, len(r) - 1))
    if observed_days and expected.get("trades_per_day"):
        lam = expected["trades_per_day"] * observed_days
        z = (n - lam) / np.sqrt(lam)
        p_values["signal_frequency"] = float(2 * norm.sf(abs(z)))
    eq = observed["pnl"].cumsum()
    base = expected.get("equity", 1.0)
    dd = float(((base + eq) / (base + eq).cummax() - 1).min())
    breach = "mc_p5_drawdown" in expected and dd < expected["mc_p5_drawdown"] * cfg.dd_breach_multiple
    out["tests"] = {k: float(v) for k, v in p_values.items()} | {"drawdown": dd, "drawdown_breach": bool(breach)}
    if "slippage_bps" in observed and expected.get("slippage_bps") is not None:
        out["tests"]["slippage_excess_bps"] = float(observed["slippage_bps"].mean() - expected["slippage_bps"])
    perf_p = min(p_values.get("win_rate", 1), p_values.get("expectancy", 1))
    health = float(np.clip(perf_p / cfg.alpha, 0, 1)) * (0.5 if breach else 1.0)
    if breach and perf_p < cfg.disable_alpha:
        rec = "DISABLE"
    elif perf_p < cfg.disable_alpha or breach:
        rec = "UNDER_REVIEW"
    elif perf_p < cfg.alpha or p_values.get("signal_frequency", 1) < cfg.frequency_alpha:
        rec = "DEGRADED"
    else:
        rec = "KEEP"
    return out | {"health": health, "recommendation": rec}
