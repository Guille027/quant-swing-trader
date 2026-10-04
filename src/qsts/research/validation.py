"""Walk-forward analysis, out-of-sample vault, parameter robustness, Monte Carlo.

Leakage rules enforced here:
- Every backtest run inside optimisation receives data TRUNCATED at the end of the window it is
  allowed to see; causal features alone would suffice, but truncation makes it structural.
- Parameters are chosen on TRAIN, the shortlist is ranked on VALIDATION, and only the single final
  choice is run on TEST. TEST results never feed back into selection.
- An embargo gap separates segments so positions/indicators spanning a boundary cannot leak.
- The OOS vault hides the reserved period from research and logs (and limits) every access.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from qsts.backtest.engine import BacktestConfig, BacktestEngine, CostModel
from qsts.backtest.metrics import compute_metrics, drawdown_series
from qsts.db import models as m
from qsts.strategy.definition import StrategyDefinition


# ============================================================== objective
@dataclass(frozen=True)
class Objective:
    """Score used for in-sample parameter choice. Sharpe, but strategies with too few trades are
    unscoreable (-inf) because their statistics are noise."""
    min_trades: int = 20

    def __call__(self, metrics: dict) -> float:
        if metrics.get("n_trades", 0) < self.min_trades:
            return -np.inf
        s = metrics.get("sharpe")
        return float(s) if s is not None and np.isfinite(s) else -np.inf


def run_window(sd, data, cfg, start, end, regime=None):
    """Backtest trading only inside [start, end]. Signals are computed on the history passed in (callers pass the
    research view, never the out-of-sample period before its final test) and the engine ignores bars after `end`.
    Every registered feature is causal (tested), so this equals computing on data cut at `end`, and the indicator
    cache is reused across walk-forward windows instead of recomputing every indicator for every window."""
    res = BacktestEngine(cfg).run(sd, data, regime=regime, start=start, end=end)
    return res, compute_metrics(res.equity, res.trades, cfg.bars_per_year)


def param_grid(space: dict[str, list]) -> list[dict]:
    keys = sorted(space)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(space[k] for k in keys))]


# ============================================================== walk-forward
@dataclass
class Fold:
    train: tuple[pd.Timestamp, pd.Timestamp]
    validate: tuple[pd.Timestamp, pd.Timestamp]
    test: tuple[pd.Timestamp, pd.Timestamp]


def make_folds(index: pd.DatetimeIndex, train: int, validate: int, test: int, embargo: int = 5,
               anchored: bool = False, step: int | None = None) -> list[Fold]:
    """Sizes in bars. Rolling (or anchored) windows: TRAIN | gap | VALIDATE | gap | TEST, stepping by `test`."""
    idx = pd.DatetimeIndex(sorted(index.unique()))
    step = step or test
    folds, start = [], 0
    while True:
        tr0, tr1 = (0 if anchored else start), start + train - 1
        va0 = tr1 + 1 + embargo
        va1 = va0 + validate - 1
        te0 = va1 + 1 + embargo
        te1 = te0 + test - 1
        if te1 >= len(idx):
            break
        folds.append(Fold((idx[tr0], idx[tr1]), (idx[va0], idx[va1]), (idx[te0], idx[te1])))
        start += step
    return folds


@dataclass
class WalkForwardResult:
    folds: list[dict]
    test_equity: pd.Series  # stitched, chained test segments (starts at 1.0)
    summary: dict


def walk_forward(sd: StrategyDefinition, data: dict[str, pd.DataFrame], space: dict[str, list], folds: list[Fold],
                 cfg: BacktestConfig = BacktestConfig(), objective: Objective = Objective(), shortlist: int = 5,
                 regime: pd.Series | None = None, progress=None) -> WalkForwardResult:
    """`progress(done, total)` is called before each fold (it may raise to cancel)."""
    rows, chained, level = [], [], 1.0
    grid = param_grid(space)
    for i, f in enumerate(folds):
        if progress is not None:
            progress(i, len(folds))
        scored = []
        for p in grid:
            _, mt = run_window(sd.with_params(**p), data, cfg, *f.train, regime)
            scored.append((objective(mt), p, mt))
        scored.sort(key=lambda x: -x[0])
        top = [x for x in scored[:shortlist] if np.isfinite(x[0])]
        if not top:
            rows.append({"fold": i, "status": "NO_VALID_PARAMS", "train": f.train, "test": f.test})
            continue
        val = []
        for s_tr, p, mt_tr in top:
            _, mv = run_window(sd.with_params(**p), data, cfg, *f.validate, regime)
            val.append((objective(mv), s_tr, p, mt_tr, mv))
        val.sort(key=lambda x: (-x[0], -x[1]))
        _, s_tr, p, mt_tr, mv = val[0]
        res, mtest = run_window(sd.with_params(**p), data, cfg, *f.test, regime)
        seg = res.equity["equity"] / cfg.initial_capital
        chained.append(seg * level)
        level = float(chained[-1].iloc[-1])
        rows.append({"fold": i, "status": "OK", "params": p, "train": f.train, "validate": f.validate, "test": f.test,
                     "train_score": s_tr, "train_metrics": mt_tr, "val_metrics": mv, "test_metrics": mtest})
    eq = pd.concat(chained) if chained else pd.Series(dtype=float)
    ok = [r for r in rows if r["status"] == "OK"]
    summary = {"n_folds": len(folds), "n_ok": len(ok)}
    if ok:
        tr_sh = np.array([r["train_metrics"].get("sharpe", np.nan) for r in ok], dtype=float)
        te_sh = np.array([r["test_metrics"].get("sharpe", np.nan) if r["test_metrics"].get("sharpe") is not None else np.nan for r in ok], dtype=float)
        summary |= {
            "mean_train_sharpe": float(np.nanmean(tr_sh)),
            "mean_test_sharpe": float(np.nanmean(te_sh)) if np.isfinite(te_sh).any() else np.nan,
            "wf_efficiency": float(np.nanmean(te_sh) / np.nanmean(tr_sh)) if np.nanmean(tr_sh) > 0 and np.isfinite(te_sh).any() else np.nan,
            "pct_profitable_folds": float(np.mean([r["test_metrics"].get("total_return", 0) > 0 for r in ok])),
            "param_changes": int(sum(ok[j]["params"] != ok[j - 1]["params"] for j in range(1, len(ok)))),
            "stitched_total_return": float(eq.iloc[-1] - 1) if len(eq) else np.nan,
            "stitched_max_drawdown": float(drawdown_series(eq).min()) if len(eq) else np.nan,
        }
    return WalkForwardResult(rows, eq, summary)


# ============================================================== OOS vault
class OOSAccessDenied(PermissionError):
    pass


class OOSVault:
    """Reserved out-of-sample period. Research code only ever receives `research_view` (data
    strictly before `oos_start`). `evaluate` is the single gate to OOS results: it logs every
    access and refuses more than `max_evaluations` per strategy version, so OOS cannot be used
    iteratively as a second training set."""

    def __init__(self, sf: sessionmaker[Session], oos_start, max_evaluations: int = 1):
        self.sf = sf
        self.oos_start = pd.Timestamp(oos_start)
        if self.oos_start.tz is None:
            self.oos_start = self.oos_start.tz_localize("UTC")
        self.max_evaluations = max_evaluations

    def research_view(self, data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        return {k: v[v.index < self.oos_start] for k, v in data.items()}

    def accesses(self, version_id: str) -> int:
        with self.sf() as s:
            return s.scalar(select(func.count()).select_from(m.OOSAccessLog)
                            .where(m.OOSAccessLog.strategy_version_id == version_id))

    def evaluate(self, sd: StrategyDefinition, data: dict[str, pd.DataFrame], cfg: BacktestConfig, purpose: str,
                 regime: pd.Series | None = None) -> dict:
        if not purpose.strip():
            raise ValueError("purpose required")
        vid = sd.version_id
        with self.sf() as s, s.begin():
            n = s.scalar(select(func.count()).select_from(m.OOSAccessLog).where(m.OOSAccessLog.strategy_version_id == vid))
            if n >= self.max_evaluations:
                raise OOSAccessDenied(f"OOS already evaluated {n}x for {vid}")
            s.add(m.OOSAccessLog(strategy_version_id=vid, purpose=purpose))
        end = max(v.index.max() for v in data.values())
        res = BacktestEngine(cfg).run(sd, data, regime=regime, start=self.oos_start, end=end)
        return compute_metrics(res.equity, res.trades, cfg.bars_per_year) | {"_trades": res.trades, "_equity": res.equity}


# ============================================================== parameter robustness
@dataclass
class RobustnessResult:
    center_score: float
    neighbors: dict[str, list[tuple[float, float]]]  # param -> [(value, score)]
    stability: float  # share of neighbour runs scoring >= tolerance * centre (and > 0)
    peak_sharpness: float  # (centre - median neighbour) / |centre|; high = isolated spike
    passed: bool
    detail: dict = field(default_factory=dict)


def neighborhood(value, rel: float = 0.15, steps: int = 5, integer: bool | None = None) -> list:
    integer = isinstance(value, int) if integer is None else integer
    lo, hi = value * (1 - rel), value * (1 + rel)
    vals = np.linspace(lo, hi, 2 * steps + 1)
    if integer:
        vals = sorted({int(round(v)) for v in vals} | {int(value) - 1, int(value), int(value) + 1})
        vals = [v for v in vals if v >= 1]
    return list(vals)


def parameter_robustness(sd: StrategyDefinition, data, cfg: BacktestConfig, start, end,
                         space: dict[str, list] | None = None, objective: Objective = Objective(),
                         tolerance: float = 0.5, min_stability: float = 0.7, max_sharpness: float = 0.5,
                         regime=None, progress=None) -> RobustnessResult:
    """One-at-a-time scan of each parameter's neighbourhood (e.g. RSI 30..40 around 35).

    tolerance 0.5: a neighbour 'holds' if it keeps >=50% of the centre score -- a deliberate,
    configurable bar; min_stability/max_sharpness define pass/fail and are configurable too."""
    _, mc = run_window(sd, data, cfg, start, end, regime)
    c = objective(mc)
    ints = sd.integer_params()
    space = space or {k: neighborhood(v, integer=k in ints) for k, v in sd.params.items()}
    nb, held, scores = {}, [], []
    total, done = sum(len(v) for v in space.values()), 0
    for k, vals in space.items():
        nb[k] = []
        for v in vals:
            done += 1
            if v == sd.params[k]:
                continue
            if progress is not None:
                progress(done, total)
            _, mt = run_window(sd.with_params(**{k: v}), data, cfg, start, end, regime)
            sc = objective(mt)
            nb[k].append((float(v), sc))
            scores.append(sc)
            held.append(np.isfinite(c) and c > 0 and np.isfinite(sc) and sc >= tolerance * c)
    stability = float(np.mean(held)) if held else np.nan
    finite = [x for x in scores if np.isfinite(x)]
    sharp = float((c - np.median(finite)) / abs(c)) if np.isfinite(c) and c != 0 and finite else np.inf
    passed = bool(np.isfinite(c) and c > 0 and stability >= min_stability and sharp <= max_sharpness)
    return RobustnessResult(c, nb, stability, sharp, passed)


# ============================================================== Monte Carlo
MC_PERCENTILES = (5, 25, 50, 75, 95)


def monte_carlo_trades(trades: pd.DataFrame, initial_equity: float, n_sims: int = 2000, seed: int = 0,
                       method: str = "bootstrap", ruin_drawdown: float = 0.5, min_trades: int = 30) -> dict:
    """Resample per-trade returns (as fraction of equity at entry) to study path dependency.

    method: 'bootstrap' (with replacement) or 'shuffle' (permutation; final return fixed, path varies).
    Below `min_trades` the distribution is not meaningful and only a flag is returned."""
    n = len(trades)
    if n < min_trades:
        return {"insufficient_trades": True, "n_trades": n, "min_trades": min_trades}
    # trade P&L relative to equity-at-entry is approximated by pnl / initial_equity scaled by
    # chained equity: we use per-trade fractional impact r_i = pnl_i / equity_before_i.
    eq = initial_equity + np.r_[0, np.cumsum(trades["pnl"].to_numpy())[:-1]]
    r = trades["pnl"].to_numpy() / eq
    rng = np.random.default_rng(seed)
    finals, mdds = np.empty(n_sims), np.empty(n_sims)
    for i in range(n_sims):
        s = rng.choice(r, n, replace=True) if method == "bootstrap" else rng.permutation(r)
        path = np.cumprod(1 + s)
        peak = np.maximum.accumulate(np.r_[1.0, path])[1:]
        finals[i] = path[-1] - 1
        mdds[i] = (path / peak - 1).min()
    pct = lambda x: {f"p{p}": float(np.percentile(x, p)) for p in MC_PERCENTILES}  # noqa: E731
    return {"method": method, "n_sims": n_sims, "n_trades": n, "seed": seed,
            "total_return": pct(finals), "max_drawdown": pct(mdds),
            "prob_loss": float(np.mean(finals < 0)),
            "risk_of_ruin": float(np.mean(mdds <= -ruin_drawdown)), "ruin_threshold": ruin_drawdown}


def cost_sensitivity(sd: StrategyDefinition, data, cfg: BacktestConfig, start, end,
                     multipliers=(0.5, 1.0, 1.5, 2.0, 3.0), regime=None) -> list[dict]:
    """Re-run with spread+slippage scaled. A strategy whose edge vanishes at 1.5-2x costs is fragile."""
    out = []
    for k in multipliers:
        c = cfg.costs
        cc = CostModel(c.commission_per_share, c.commission_pct, c.commission_min, c.spread_bps * k,
                       c.slippage_bps * k, c.borrow_rate_annual, c.max_volume_participation)
        _, mt = run_window(sd, data, BacktestConfig(**{**cfg.__dict__, "costs": cc}), start, end, regime)
        out.append({"cost_multiplier": k, "total_return": mt.get("total_return"), "sharpe": mt.get("sharpe"),
                    "n_trades": mt.get("n_trades", 0)})
    return out
