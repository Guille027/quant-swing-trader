"""Feature evaluation & selection.

"More indicators" is not "more accuracy". These tools measure whether a feature carries information
beyond what other features already provide, and whether that information is stable over time.

Labels (forward returns) look into the future BY DEFINITION. They are only ever produced by
`forward_returns` and must only be evaluated on a training/validation slice -- callers pass the
slice explicitly; nothing here touches the reserved OOS set.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def forward_returns(close: pd.Series, horizon: int) -> pd.Series:
    """LABEL (future information). Return from close[t] to close[t+horizon]."""
    return close.shift(-horizon) / close - 1


def information_coefficient(feature: pd.Series, label: pd.Series) -> float:
    d = pd.concat([feature, label], axis=1).dropna()
    if len(d) < 30:
        return float("nan")
    return float(d.iloc[:, 0].rank().corr(d.iloc[:, 1].rank()))


def ic_stability(feature: pd.Series, label: pd.Series, n_chunks: int = 5) -> dict:
    """IC per contiguous chunk. A real effect should keep its sign across most chunks."""
    d = pd.concat([feature.rename("f"), label.rename("y")], axis=1).dropna()
    chunks = np.array_split(np.arange(len(d)), n_chunks)
    ics = [information_coefficient(d["f"].iloc[c], d["y"].iloc[c]) for c in chunks if len(c) >= 30]
    ics = np.array([x for x in ics if np.isfinite(x)])
    if len(ics) == 0:
        return {"ic_mean": np.nan, "ic_std": np.nan, "sign_consistency": np.nan, "ics": []}
    sign = np.sign(np.mean(ics)) if np.mean(ics) != 0 else 1
    return {"ic_mean": float(ics.mean()), "ic_std": float(ics.std(ddof=1)) if len(ics) > 1 else np.nan,
            "sign_consistency": float(np.mean(np.sign(ics) == sign)), "ics": ics.tolist()}


def redundancy(features: pd.DataFrame, threshold: float = 0.9) -> list[tuple[str, str, float]]:
    """Pairs whose |Spearman correlation| exceeds threshold (multicollinearity candidates)."""
    c = features.rank().corr().abs()
    out = []
    cols = list(c.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            if c.loc[a, b] >= threshold:
                out.append((a, b, float(c.loc[a, b])))
    return sorted(out, key=lambda x: -x[2])


def _fit_ridge(X: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    Xb = np.c_[np.ones(len(X)), X]
    reg = lam * np.eye(Xb.shape[1])
    reg[0, 0] = 0
    return np.linalg.solve(Xb.T @ Xb + reg, Xb.T @ y)


def _predict(w, X):
    return np.c_[np.ones(len(X)), X] @ w


def _score(pred, y) -> float:
    return float(pd.Series(pred).rank().corr(pd.Series(y).rank()))


@dataclass
class ImportanceResult:
    baseline_score: float
    permutation: dict[str, float]  # drop in validation rank-IC when the feature is shuffled
    ablation: dict[str, float]  # drop in validation rank-IC when the feature is removed & model refit


def permutation_and_ablation(
    train_X: pd.DataFrame, train_y: pd.Series, val_X: pd.DataFrame, val_y: pd.Series,
    seed: int, n_repeats: int = 10, ridge_lambda: float = 1.0,
) -> ImportanceResult:
    """Fit on train only; measure on validation only. Standardisation uses train statistics."""
    tr = pd.concat([train_X, train_y.rename("_y")], axis=1).dropna()
    va = pd.concat([val_X, val_y.rename("_y")], axis=1).dropna()
    cols = list(train_X.columns)
    mu, sd = tr[cols].mean(), tr[cols].std().replace(0, 1)
    Xt, yt = ((tr[cols] - mu) / sd).to_numpy(), tr["_y"].to_numpy()
    Xv, yv = ((va[cols] - mu) / sd).to_numpy(), va["_y"].to_numpy()
    w = _fit_ridge(Xt, yt, ridge_lambda)
    base = _score(_predict(w, Xv), yv)
    rng = np.random.default_rng(seed)
    perm, abl = {}, {}
    for j, c in enumerate(cols):
        drops = []
        for _ in range(n_repeats):
            Xp = Xv.copy()
            Xp[:, j] = rng.permutation(Xp[:, j])
            drops.append(base - _score(_predict(w, Xp), yv))
        perm[c] = float(np.mean(drops))
        keep = [k for k in range(len(cols)) if k != j]
        if keep:
            wk = _fit_ridge(Xt[:, keep], yt, ridge_lambda)
            abl[c] = base - _score(_predict(wk, Xv[:, keep]), yv)
        else:
            abl[c] = base
    return ImportanceResult(base, perm, abl)


@dataclass(frozen=True)
class SelectionConfig:
    redundancy_threshold: float = 0.9  # |rho| above which two features are near-duplicates
    min_abs_ic: float = 0.01  # below this, signal is indistinguishable from noise for typical N
    min_sign_consistency: float = 0.6  # IC sign must hold in >=60% of chunks


def select_features(train_features: pd.DataFrame, train_label: pd.Series, cfg: SelectionConfig = SelectionConfig()) -> dict:
    """Return kept / dropped features with the reason for each drop. Training data only."""
    stats = {c: ic_stability(train_features[c], train_label) for c in train_features.columns}
    dropped: dict[str, str] = {}
    for c, s in stats.items():
        if not np.isfinite(s["ic_mean"]) or abs(s["ic_mean"]) < cfg.min_abs_ic:
            dropped[c] = f"weak IC ({s['ic_mean']:.4f})"
        elif s["sign_consistency"] < cfg.min_sign_consistency:
            dropped[c] = f"unstable IC sign ({s['sign_consistency']:.2f})"
    remaining = [c for c in train_features.columns if c not in dropped]
    for a, b, rho in redundancy(train_features[remaining], cfg.redundancy_threshold):
        if a in dropped or b in dropped:
            continue
        weaker = a if abs(stats[a]["ic_mean"]) < abs(stats[b]["ic_mean"]) else b
        dropped[weaker] = f"redundant with {b if weaker == a else a} (rho={rho:.2f})"
    return {"kept": [c for c in train_features.columns if c not in dropped], "dropped": dropped, "stats": stats}
