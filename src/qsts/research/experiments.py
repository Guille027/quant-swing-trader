"""Experiment tracking & exact reproduction.

An experiment stores everything needed to re-run it: strategy definition (by version id), dataset
version (content hashes of every validated frame), engine config, period, seed and code version.
`reproduce` re-runs it from the stored record and verifies the metrics are bit-identical.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session, sessionmaker

from qsts.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult, CostModel
from qsts.backtest.metrics import compute_metrics
from qsts.core.hashing import hash_frame, hash_obj
from qsts.db import models as m
from qsts.strategy.definition import StrategyDefinition, definition_from_dict


def code_version() -> str:
    try:
        root = Path(__file__).resolve().parents[3]
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "src"],
                               capture_output=True, text=True, timeout=5)
        if sha.returncode == 0:
            return sha.stdout.strip()[:12] + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        pass
    from importlib.metadata import version
    return "pkg-" + version("qsts")


def dataset_fingerprint(data: dict[str, pd.DataFrame]) -> dict:
    return {sym: hash_frame(df) for sym, df in sorted(data.items())}


def _clean(obj):
    """JSON-safe metrics (NaN/inf -> None/str)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        if np.isnan(obj):
            return None
        if np.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def config_from_dict(d: dict) -> BacktestConfig:
    d = dict(d)
    d["costs"] = CostModel(**d["costs"])
    return BacktestConfig(**d)


@dataclass
class ExperimentRecord:
    id: str
    result: BacktestResult
    metrics: dict


class ExperimentTracker:
    def __init__(self, sf: sessionmaker[Session]):
        self.sf = sf

    def _ensure_strategy(self, s: Session, sd: StrategyDefinition, strategy_id: str) -> None:
        if s.get(m.Strategy, strategy_id) is None:
            s.add(m.Strategy(id=strategy_id, name=sd.name, family=sd.family))
            s.add(m.StrategyStatusHistory(strategy_id=strategy_id, to_status="RESEARCH", reason="created by experiment",
                                          actor="system"))
            s.flush()
        if s.get(m.StrategyVersion, sd.version_id) is None:
            n = s.query(m.StrategyVersion).filter_by(strategy_id=strategy_id).count()
            s.add(m.StrategyVersion(id=sd.version_id, strategy_id=strategy_id, version=n + 1, definition=sd.to_dict()))

    def run_backtest(self, sd: StrategyDefinition, data: dict[str, pd.DataFrame], cfg: BacktestConfig,
                     *, start=None, end=None, seed: int = 0, strategy_id: str | None = None,
                     kind: str = "backtest", extra: dict | None = None, regime: pd.Series | None = None) -> ExperimentRecord:
        res = BacktestEngine(cfg).run(sd, data, regime=regime, start=start, end=end)
        metrics = _clean(compute_metrics(res.equity, res.trades, cfg.bars_per_year))
        ds_spec = {"frames": dataset_fingerprint(data)}
        ds_id = hash_obj(ds_spec, 32)
        exp_cfg = {"engine": cfg.to_dict(), "start": str(start) if start is not None else None,
                   "end": str(end) if end is not None else None, "symbols": sorted(data),
                   "features": [s.to_dict() for s in sd.feature_set().specs], "timeframe": sd.timeframe,
                   "params": sd.params, "regime_hash": hash_frame(regime.to_frame()) if regime is not None else None,
                   **(extra or {})}
        cv = code_version()
        exp_id = hash_obj({"sv": sd.version_id, "ds": ds_id, "cfg": exp_cfg, "seed": seed, "kind": kind, "code": cv}, 32)
        sid = strategy_id or sd.name
        with self.sf() as s, s.begin():
            if s.get(m.DatasetVersion, ds_id) is None:
                s.add(m.DatasetVersion(id=ds_id, spec=ds_spec))
            self._ensure_strategy(s, sd, sid)
            if s.get(m.Experiment, exp_id) is None:
                s.add(m.Experiment(id=exp_id, strategy_version_id=sd.version_id, dataset_version_id=ds_id, kind=kind,
                                   config=_clean(exp_cfg), seed=seed, code_version=cv, metrics=metrics))
                s.flush()
                eq = res.equity
                s.add(m.Backtest(
                    experiment_id=exp_id,
                    start=eq.index[0].to_pydatetime().replace(tzinfo=None) if len(eq) else None,
                    end=eq.index[-1].to_pydatetime().replace(tzinfo=None) if len(eq) else None,
                    metrics=metrics,
                    trades=_clean(res.trades.astype({c: str for c in ("entry_ts", "exit_ts") if c in res.trades}).to_dict("records")),
                    equity_curve=_clean([[str(t), float(v)] for t, v in eq["equity"].items()])))
        return ExperimentRecord(exp_id, res, metrics)

    def get(self, exp_id: str) -> m.Experiment:
        with self.sf() as s:
            e = s.get(m.Experiment, exp_id)
            if e is None:
                raise KeyError(exp_id)
            return e

    def reproduce(self, exp_id: str, data: dict[str, pd.DataFrame], regime: pd.Series | None = None) -> dict:
        """REPRODUCE EXPERIMENT: same data (verified by hash), strategy, params, config, period, seed."""
        e = self.get(exp_id)
        with self.sf() as s:
            sv = s.get(m.StrategyVersion, e.strategy_version_id)
            ds = s.get(m.DatasetVersion, e.dataset_version_id)
        data = {k: data[k] for k in e.config["symbols"] if k in data}
        fp = dataset_fingerprint(data)
        if fp != ds.spec["frames"] and e.config.get("end"):
            # experiments run on a research view (data cut at the OOS boundary): rebuild that view
            end_ts = pd.Timestamp(e.config["end"])
            cut = {k: v[v.index <= end_ts] for k, v in data.items()}
            if dataset_fingerprint(cut) == ds.spec["frames"]:
                data, fp = cut, ds.spec["frames"]
        if fp != ds.spec["frames"]:
            bad = sorted(k for k in ds.spec["frames"] if fp.get(k) != ds.spec["frames"][k])
            return {"reproduced": False, "reason": f"dataset differs for {bad}"}
        sd = definition_from_dict(sv.definition)
        if sd.version_id != e.strategy_version_id:
            return {"reproduced": False, "reason": "strategy definition hash mismatch"}
        cfg = config_from_dict(e.config["engine"])
        start = pd.Timestamp(e.config["start"]) if e.config["start"] else None
        end = pd.Timestamp(e.config["end"]) if e.config["end"] else None
        res = BacktestEngine(cfg).run(sd, {k: data[k] for k in e.config["symbols"]}, regime=regime, start=start, end=end)
        metrics = _clean(compute_metrics(res.equity, res.trades, cfg.bars_per_year))
        same = metrics == e.metrics
        return {"reproduced": same, "metrics": metrics, "stored_metrics": e.metrics,
                "code_version_now": code_version(), "code_version_then": e.code_version,
                "reason": None if same else "metrics differ"}
