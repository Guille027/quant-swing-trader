"""Experimental evolutionary strategy search.

Population -> evaluate -> select -> crossover -> mutate -> new generation.

Anti-overfitting design:
- Fitness uses TWO disjoint research windows (train, validation) and takes the MINIMUM of their
  scores, so a rule set must work on both; it never sees the OOS vault.
- Explicit penalties for complexity (params + rules + features) and for train/validation divergence.
- Thresholds are parameters ("$p0"...) sampled from TRAIN-window feature quantiles only.
- Every evaluated genome is counted: `n_trials` feeds the Deflated Sharpe Ratio downstream, so a
  bigger search is penalised for its larger multiple-testing burden.
- Results are only *hypotheses*: survivors must still pass the full pipeline (WF, robustness, OOS).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qsts.backtest.engine import BacktestConfig
from qsts.features.registry import REGISTRY, FeatureSpec, FeatureSet
from qsts.research.validation import Objective, run_window
from qsts.strategy.definition import Condition, Operand, StopRule, StrategyDefinition, TakeProfitRule

EXCLUDED = {"sma", "ema", "hma", "atr", "obv_slope"}  # raw price-level features are not comparable to thresholds


@dataclass(frozen=True)
class EvolutionConfig:
    population: int = 30
    generations: int = 10
    elite: int = 3
    tournament: int = 3
    crossover_rate: float = 0.5
    mutation_rate: float = 0.8
    max_conditions: int = 3
    complexity_penalty: float = 0.03  # Sharpe units per complexity point
    divergence_penalty: float = 0.5  # Sharpe units per unit of |train - val| gap
    direction: str = "long"
    seed: int = 0
    objective: Objective = field(default_factory=Objective)


@dataclass
class Individual:
    sd: StrategyDefinition
    fitness: float = -np.inf
    train: float = -np.inf
    val: float = -np.inf


class EvolutionEngine:
    def __init__(self, data: dict[str, pd.DataFrame], train: tuple, validate: tuple | None,
                 bt_cfg: BacktestConfig = BacktestConfig(), cfg: EvolutionConfig = EvolutionConfig()):
        """`validate` may be None only for subclasses that override `evaluate` with their own fitness."""
        if validate is not None and pd.Timestamp(train[1]) >= pd.Timestamp(validate[0]):
            raise ValueError("train must end before validation starts")
        self.data, self.train, self.validate = data, train, validate
        self.bt, self.cfg = bt_cfg, cfg
        self.rng = np.random.default_rng(cfg.seed)
        self.n_trials = 0
        self.history: list[dict] = []
        self._cache: dict[str, Individual] = {}
        self.features = sorted(k for k, d in REGISTRY.items()
                               if not k.startswith("_") and not d.needs_benchmark and k not in EXCLUDED)
        self._quantiles = self._feature_quantiles()

    def _feature_quantiles(self) -> dict[str, np.ndarray]:
        """Per-feature quantiles computed on the TRAIN window only."""
        fs = FeatureSet([FeatureSpec(f) for f in self.features])
        parts = []
        for df in self.data.values():
            d = df[df.index <= pd.Timestamp(self.train[1])]
            parts.append(fs.compute(d)[d.index >= pd.Timestamp(self.train[0])])
        allf = pd.concat(parts)
        qs = np.linspace(0.05, 0.95, 19)
        out = {}
        for spec in fs.specs:
            col = allf[spec.key].dropna()
            if col.nunique() > 1:
                out[spec.name] = np.unique(np.quantile(col, qs))
        self.features = [f for f in self.features if f in out]
        return out

    # ------------------------------------------------------------------ genome ops
    def _rand_condition(self, params: dict) -> Condition:
        f = str(self.rng.choice(self.features))
        q = self._quantiles[f]
        name = f"p{len(params)}"
        while name in params:
            name = f"p{int(name[1:]) + 1}"
        params[name] = float(q[self.rng.integers(len(q))])
        op = str(self.rng.choice(["<", ">"]))
        return Condition(Operand(feature=f), op, Operand(value=f"${name}"))

    def random_individual(self) -> StrategyDefinition:
        params: dict = {}
        k = int(self.rng.integers(1, self.cfg.max_conditions + 1))
        conds = tuple(self._rand_condition(params) for _ in range(k))
        return self._build(conds, params, stop_mult=float(self.rng.choice([1.5, 2.0, 2.5, 3.0])),
                           tp=float(self.rng.choice([0.0, 1.5, 2.0, 3.0])), hold=int(self.rng.choice([5, 10, 20])))

    def _build(self, conds, params, stop_mult, tp, hold) -> StrategyDefinition:
        used = {c.right.value[1:] for c in conds if isinstance(c.right.value, str)}
        params = {k: v for k, v in params.items() if k in used}
        long = self.cfg.direction == "long"
        return StrategyDefinition(
            name="evo", family="evolved", hypothesis="evolutionary search candidate (unvalidated)",
            direction=self.cfg.direction, entry_long=conds if long else (), entry_short=() if long else conds,
            stop=StopRule("atr", 14, stop_mult), take_profit=TakeProfitRule("none") if tp == 0 else TakeProfitRule("r_multiple", tp),
            max_holding_bars=hold, params=params, metadata={"origin": "evolution", "seed": self.cfg.seed})

    def _conds(self, sd):
        return sd.entry_long if self.cfg.direction == "long" else sd.entry_short

    def mutate(self, sd: StrategyDefinition) -> StrategyDefinition:
        conds, params = list(self._conds(sd)), dict(sd.params)
        stop, tp = float(sd.stop.mult), (0.0 if sd.take_profit.kind == "none" else float(sd.take_profit.value))
        hold = int(sd.max_holding_bars or 10)
        r = self.rng.random()
        if r < 0.4 and params:  # shift a threshold to a neighbouring train quantile
            c = conds[int(self.rng.integers(len(conds)))]
            name = c.right.value[1:]
            q = self._quantiles[c.left.feature]
            j = int(np.clip(np.searchsorted(q, params[name]) + self.rng.choice([-2, -1, 1, 2]), 0, len(q) - 1))
            params[name] = float(q[j])
        elif r < 0.55:
            i = int(self.rng.integers(len(conds)))
            c = conds[i]
            conds[i] = Condition(c.left, ">" if c.op == "<" else "<", c.right)
        elif r < 0.7 and len(conds) < self.cfg.max_conditions:
            conds.append(self._rand_condition(params))
        elif r < 0.8 and len(conds) > 1:
            conds.pop(int(self.rng.integers(len(conds))))
        elif r < 0.9:
            stop = float(np.clip(stop + self.rng.choice([-0.5, 0.5]), 1.0, 4.0))
        else:
            hold = int(self.rng.choice([3, 5, 10, 15, 20]))
            tp = float(self.rng.choice([0.0, 1.5, 2.0, 3.0]))
        return self._build(tuple(conds), params, stop, tp, hold)

    def crossover(self, a: StrategyDefinition, b: StrategyDefinition) -> StrategyDefinition:
        params, conds = {}, []
        for c in self._conds(a) + self._conds(b):
            if self.rng.random() < 0.5 and len(conds) < self.cfg.max_conditions:
                src = a if c in self._conds(a) else b
                name = f"p{len(params)}"
                params[name] = src.params[c.right.value[1:]]
                conds.append(Condition(c.left, c.op, Operand(value=f"${name}")))
        if not conds:
            return copy.deepcopy(a)
        return self._build(tuple(conds), params, float(a.stop.mult),
                           0.0 if b.take_profit.kind == "none" else float(b.take_profit.value), int(b.max_holding_bars or 10))

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, sd: StrategyDefinition) -> Individual:
        vid = sd.version_id
        if vid in self._cache:
            return self._cache[vid]
        self.n_trials += 1
        _, mt = run_window(sd, self.data, self.bt, *self.train)
        _, mv = run_window(sd, self.data, self.bt, *self.validate)
        st, sv = self.cfg.objective(mt), self.cfg.objective(mv)
        cx = sd.complexity()["score"]
        if np.isfinite(st) and np.isfinite(sv):
            fit = min(st, sv) - self.cfg.complexity_penalty * cx - self.cfg.divergence_penalty * abs(st - sv)
        else:
            fit = -np.inf
        ind = Individual(sd, fit, st, sv)
        self._cache[vid] = ind
        return ind

    def _tournament(self, pop: list[Individual]) -> Individual:
        picks = self.rng.choice(len(pop), size=min(self.cfg.tournament, len(pop)), replace=False)
        return max((pop[i] for i in picks), key=lambda x: x.fitness)

    def run(self, initial: list[StrategyDefinition] | None = None) -> dict:
        """`initial`: genomes to seed the population with (e.g. the best found in earlier runs), so the
        search builds on what it already learnt. They must use this engine's genome shape."""
        seeds = [self.evaluate(sd) for sd in (initial or [])[: self.cfg.population]]
        pop = seeds + [self.evaluate(self.random_individual()) for _ in range(self.cfg.population - len(seeds))]
        for g in range(self.cfg.generations):
            pop.sort(key=lambda x: -x.fitness)
            finite = [p.fitness for p in pop if np.isfinite(p.fitness)]
            self.history.append({"generation": g, "best": pop[0].fitness, "median": float(np.median(finite)) if finite else None,
                                 "n_valid": len(finite), "n_trials": self.n_trials})
            nxt = pop[: self.cfg.elite]
            while len(nxt) < self.cfg.population:
                a = self._tournament(pop).sd
                child = self.crossover(a, self._tournament(pop).sd) if self.rng.random() < self.cfg.crossover_rate else a
                if self.rng.random() < self.cfg.mutation_rate:
                    child = self.mutate(child)
                nxt.append(self.evaluate(child))
            pop = nxt
        pop.sort(key=lambda x: -x.fitness)
        best = [p for p in pop if np.isfinite(p.fitness)]
        seen, uniq = set(), []
        for p in best:
            if p.sd.version_id not in seen:
                seen.add(p.sd.version_id)
                uniq.append(p)
        return {"best": uniq[:5], "history": self.history, "n_trials": self.n_trials,
                "note": "Candidates are unvalidated hypotheses; run them through the research pipeline."}
