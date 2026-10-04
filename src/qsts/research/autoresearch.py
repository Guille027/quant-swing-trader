"""Automatic strategy research loop ("Investigación IA").

Each cycle:
  1. evolutionary search, seeded with the best evolved strategies found so far (it builds on them);
  2. optionally the AI reads a summary of what worked and what failed and proposes new hypotheses;
  3. every candidate is scored for CONSISTENCY on the research period only and stored;
  4. the best new candidates are validated (walk-forward, parameter robustness, Monte Carlo, 2x costs,
     baselines, overfitting risk computed with the GLOBAL number of trials).

Integrity rules:
- Only research-period data (strictly before the OOS boundary) is used here. The OOS vault is opened only
  by `final_test`, explicitly, once per strategy version; its results never reach the search or the AI.
- Every evaluated candidate is persisted and counted. The leaderboard's Deflated Sharpe Ratio uses the
  total number of trials, so a longer search has to clear a higher bar ("more tries = more luck").
- Consistency fitness = the WORST annualised Sharpe over `blocks` consecutive sub-periods, minus a
  complexity penalty: a rule set must work in every era, not just on average.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from qsts.ai.providers import AIProviderError
from qsts.ai.service import AIBudgetExceeded, AIOutputRejected, AIResearchService
from qsts.backtest.benchmarks import momentum_baseline, trend_baseline
from qsts.backtest.engine import ENGINE_VERSION, BacktestConfig
from qsts.backtest.metrics import periodic_returns
from qsts.core.hashing import hash_obj
from qsts.core.power import keep_awake
from qsts.db import models as m
from qsts.features.registry import REGISTRY, feature_cache
from qsts.research.evolution import EvolutionConfig, EvolutionEngine, Individual
from qsts.research.experiments import ExperimentTracker, _clean, dataset_fingerprint
from qsts.research.scoring import (ScoreConfig, expected_max_sharpe, overfitting_risk, psr_from_stats, return_stats,
                                   strategy_score)
from qsts.research.validation import (OOSVault, cost_sensitivity, make_folds, monte_carlo_trades, parameter_robustness,
                                      run_window, walk_forward)
from qsts.strategy.definition import StrategyDefinition, definition_from_dict
from qsts.strategy.lifecycle import Status, StrategyRegistry


@dataclass(frozen=True)
class AutoResearchConfig:
    oos_start: str = "2023-01-01"
    warmup_bars: int = 252          # indicator warm-up before scoring starts (data before it is still used)
    blocks: int = 3                 # consecutive sub-periods for the consistency score
    min_trades: int = 30
    min_block_trades: int = 5
    complexity_penalty: float = 0.03
    max_holding_days: int = 20      # swing trading: every position is closed after at most this many sessions
    population: int = 20
    generations: int = 4
    elites_from_history: int = 6
    finalists_per_cycle: int = 2
    finalist_pool: int = 10         # only candidates in the global top-N are validated
    use_ai: bool = True
    ai_proposals: int = 3
    avoid_earnings: bool = True     # apply the earnings blackout / exit-before-results rules (Settings) in research
    seed: int = 0
    wf_train: int = 504
    wf_validate: int = 126
    wf_test: int = 126
    embargo: int = 5
    mc_sims: int = 1000


class StopRequested(Exception):
    pass


FINAL_CRITERIA_VERSION = 2
FINAL_MIN_SHARPE_RETENTION = 0.5  # OOS Sharpe must keep at least half of the research Sharpe


def window_stats(rets: pd.Series, bars_per_year: int = 252) -> dict:
    rets = pd.Series(rets).fillna(0.0)
    if len(rets) < 2:
        return {"total_return": None, "cagr": None, "sharpe": None, "max_drawdown": None}
    eq = (1 + rets).cumprod()
    sd = rets.std(ddof=1)
    return {"total_return": float(eq.iloc[-1] - 1),
            "cagr": float(eq.iloc[-1] ** (bars_per_year / len(rets)) - 1) if eq.iloc[-1] > 0 else -1.0,
            "sharpe": float(rets.mean() / sd * np.sqrt(bars_per_year)) if sd > 0 else 0.0,
            "max_drawdown": float((eq / eq.cummax() - 1).min())}


def final_verdict(oos: dict, research_sharpe: float | None, passive: dict | None) -> dict:
    """The one-time out-of-sample test passes only if the strategy (1) made money, (2) beat holding the same stocks
    and doing nothing (risk-adjusted), and (3) kept at least half of its research Sharpe."""
    sh, ret = oos.get("sharpe"), oos.get("total_return")
    checks = {
        "positive": ret is not None and ret > 0,
        "beats_passive": sh is not None and passive is not None and passive.get("sharpe") is not None
                         and sh >= passive["sharpe"],
        "limited_decay": sh is not None and research_sharpe is not None and research_sharpe > 0
                         and sh >= FINAL_MIN_SHARPE_RETENTION * research_sharpe,
    }
    return {"checks": checks, "passed": all(checks.values())}


# ---------------------------------------------------------------------- human-readable rules
_OPS = {"<": "<", ">": ">", "<=": "≤", ">=": "≥", "cross_above": "cruza por encima de", "cross_below": "cruza por debajo de"}


def _num(x) -> str:
    return f"{x:.4g}" if isinstance(x, (int, float, np.floating)) else str(x)


def _operand(sd: StrategyDefinition, o) -> str:
    if o.feature:
        params = {**(REGISTRY[o.feature].defaults if o.feature in REGISTRY else {}),
                  **{k: sd.resolve(v) for k, v in o.params.items()}}
        return f"{o.feature}({','.join(_num(v) for v in params.values())})" if params else o.feature
    return _num(sd.resolve(o.value))


def describe(sd: StrategyDefinition) -> str:
    """Plain-language summary of a strategy's rules (Spanish, for the UI and the AI context)."""
    cond = lambda c: f"{_operand(sd, c.left)} {_OPS.get(c.op, c.op)} {_operand(sd, c.right)}"  # noqa: E731
    parts = []
    if sd.entry_long:
        parts.append("Compra si " + " y ".join(cond(c) for c in sd.entry_long))
    if sd.entry_short:
        parts.append("Vende en corto si " + " y ".join(cond(c) for c in sd.entry_short))
    if sd.exit_long:
        parts.append("sale si " + " o ".join(cond(c) for c in sd.exit_long))
    s = sd.stop
    if s.kind == "atr":
        parts.append(f"stop {_num(sd.resolve(s.mult))}×ATR({_num(sd.resolve(s.atr_n))})")
    elif s.kind == "percent":
        parts.append(f"stop {_num(100 * float(sd.resolve(s.mult)))}%")
    else:
        parts.append(f"stop bajo el último mínimo ({_num(sd.resolve(s.structure_k))} barras)")
    tp = sd.take_profit
    if tp.kind == "r_multiple":
        parts.append(f"objetivo {_num(sd.resolve(tp.value))}R")
    elif tp.kind == "atr":
        parts.append(f"objetivo {_num(sd.resolve(tp.value))}×ATR")
    if sd.max_holding_bars:
        parts.append(f"máx. {_num(sd.resolve(sd.max_holding_bars))} días")
    return "; ".join(parts)


# ---------------------------------------------------------------------- consistency score
class ConsistencyEvaluator:
    """One backtest over the research window, then the equity curve is cut into consecutive blocks."""

    def __init__(self, research: dict[str, pd.DataFrame], start, end, bt_cfg: BacktestConfig, cfg: AutoResearchConfig):
        self.data, self.start, self.end, self.bt, self.cfg = research, start, end, bt_cfg, cfg

    def evaluate(self, sd: StrategyDefinition) -> tuple[float | None, dict]:
        res, mt = run_window(sd, self.data, self.bt, self.start, self.end)
        eq = res.equity["equity"]
        rets = eq.pct_change().dropna()
        tr = res.trades
        entries = pd.DatetimeIndex(tr["entry_ts"]) if len(tr) else pd.DatetimeIndex([], tz="UTC")
        blocks = []
        for b in np.array_split(np.arange(len(rets)), self.cfg.blocks):
            r = rets.iloc[b]
            sdev = r.std(ddof=1)
            blocks.append({"start": str(r.index[0].date()), "end": str(r.index[-1].date()),
                           "sharpe": float(r.mean() / sdev * np.sqrt(self.bt.bars_per_year)) if sdev > 0 else 0.0,
                           "return": float((1 + r).prod() - 1),
                           "trades": int(((entries >= r.index[0]) & (entries <= r.index[-1])).sum())})
        yearly = periodic_returns(eq, "YE")
        st = return_stats(rets) or {}
        cx = sd.complexity()["score"]
        metrics = {k: mt.get(k) for k in ("cagr", "sharpe", "max_drawdown", "n_trades", "win_rate", "profit_factor",
                                          "exposure", "avg_trade_bars", "total_costs")}
        metrics |= {"blocks": blocks, "pct_positive_years": float((yearly > 0).mean()) if len(yearly) else None,
                    "worst_year": float(yearly.min()) if len(yearly) else None,
                    "yearly": {str(k.year): float(v) for k, v in yearly.items()}, "complexity": cx,
                    "period": [str(pd.Timestamp(self.start).date()), str(pd.Timestamp(self.end).date())], **st}
        nt = mt.get("n_trades", 0) or 0
        if nt < self.cfg.min_trades:
            metrics["invalid_reason"] = f"pocas operaciones ({nt} < {self.cfg.min_trades})"
            return None, _clean(metrics)
        thin = [b for b in blocks if b["trades"] < self.cfg.min_block_trades]
        if thin:
            metrics["invalid_reason"] = f"casi sin operaciones en {thin[0]['start']}→{thin[0]['end']}"
            return None, _clean(metrics)
        fit = min(b["sharpe"] for b in blocks) - self.cfg.complexity_penalty * cx
        return float(fit), _clean(metrics)


class _ConsistencyEvolution(EvolutionEngine):
    """Evolution whose fitness is the consistency score; every evaluation is persisted (= one trial)."""

    def __init__(self, owner: "AutoResearcher", data, window, bt_cfg, cfg):
        super().__init__(data, window, None, bt_cfg, cfg)
        self.owner, self.cycle = owner, 0

    def evaluate(self, sd: StrategyDefinition) -> Individual:
        vid = sd.version_id
        if vid in self._cache:
            return self._cache[vid]
        self.owner.check_stop()
        fit, _, new = self.owner.evaluate_and_store(sd, "evolution", self.cycle)
        self.n_trials += int(new)
        ind = Individual(sd, fit if fit is not None else -np.inf)
        self._cache[vid] = ind
        return ind


def _engine_key(bt: BacktestConfig) -> dict:
    """Engine config for the ranking id; earnings options at their 'off' defaults are omitted so rankings made
    before those options existed stay valid."""
    d = bt.to_dict()
    for k, off in (("earnings_blackout_days", 0), ("exit_before_earnings", False)):
        if d.get(k) == off:
            d.pop(k, None)
    return d


def _earnings_key(research: dict[str, pd.DataFrame]) -> str | None:
    """Fingerprint of the earnings columns inside the research window (None = no earnings data at all)."""
    from qsts.core.hashing import hash_frame
    from qsts.data.earnings import EARN_COLS
    fp = {k: hash_frame(v[EARN_COLS]) for k, v in sorted(research.items()) if set(EARN_COLS) <= set(v.columns)}
    return hash_obj(fp, 16) if fp else None


# ---------------------------------------------------------------------- the loop
class AutoResearcher:
    def __init__(self, sf, data: dict[str, pd.DataFrame], cfg: AutoResearchConfig = AutoResearchConfig(),
                 bt_cfg: BacktestConfig = BacktestConfig(), ai: AIResearchService | None = None,
                 log: Callable[[str], None] | None = None, stop_event: threading.Event | None = None,
                 benchmark: pd.Series | None = None):
        """`data`: adjusted frames incl. the OOS period (only the vault reads past the boundary).
        `benchmark`: adjusted close of the benchmark (SPY), only for the buy & hold comparison in backtest views."""
        self.sf, self.cfg, self.bt, self.ai = sf, cfg, bt_cfg, ai
        self.vault = OOSVault(sf, cfg.oos_start)
        self.full = data
        self.research = {k: v for k, v in self.vault.research_view(data).items() if len(v)}
        idx = pd.DatetimeIndex(sorted(set().union(*[d.index for d in self.research.values()])))
        if len(idx) <= cfg.warmup_bars + 252:
            raise ValueError("not enough research-period data before the OOS boundary")
        self.index = idx[cfg.warmup_bars:]
        self.start, self.end = self.index[0], self.index[-1]
        self.evaluator = ConsistencyEvaluator(self.research, self.start, self.end, bt_cfg, cfg)
        self.dataset_id = hash_obj(dataset_fingerprint(self.research), 32)
        self.benchmark = benchmark
        # a ranking only compares strategies scored on the same symbols, window and scoring rules
        key = {"symbols": {k: str(v.index[0].date()) for k, v in sorted(self.research.items())},
               "oos_start": cfg.oos_start, "warmup": cfg.warmup_bars, "blocks": cfg.blocks,
               "min_trades": cfg.min_trades, "min_block_trades": cfg.min_block_trades,
               "complexity_penalty": cfg.complexity_penalty, "engine": _engine_key(bt_cfg),
               "engine_version": ENGINE_VERSION}
        if bt_cfg.earnings_blackout_days or bt_cfg.exit_before_earnings:
            # the earnings rules make scores depend on the earnings data; with the rules off, strategies that do not
            # use earnings features score identically, so earlier rankings stay valid
            key["earnings_data"] = _earnings_key(self.research)
        self.universe_id = hash_obj(key, 32)
        self.registry, self.tracker = StrategyRegistry(sf), ExperimentTracker(sf)
        self.log = log or (lambda msg: None)
        self.stop_event = stop_event or threading.Event()
        self.phase = "idle"
        self.session_trials = 0
        self._engine: _ConsistencyEvolution | None = None
        self._last_ai_rejections: list[str] = []
        self._imported = False
        self._adopt_legacy_rows()
        self.rejudge_finals()

    def _adopt_legacy_rows(self) -> None:
        """Rows stored before rankings were scoped by universe were scored by engine v1: they keep counting as trials
        (Deflated Sharpe) but are never mixed into a current ranking."""
        R = m.ResearchCandidate
        with self.sf() as s, s.begin():
            for r in s.scalars(select(R).where(R.universe_id.is_(None), R.version_id.is_(None))).all():
                r.version_id = r.id

    def passive_reference(self) -> dict:
        """'Do nothing' benchmark on the SAME stocks: hold all of them, equal weight, rebalanced daily, no costs
        (favours the benchmark). Scored exactly like a candidate (worst of the same blocks)."""
        if getattr(self, "_passive", None) is None:
            rets = pd.concat({k: v["close"].pct_change() for k, v in self.research.items()}, axis=1)
            rets = rets[(rets.index >= self.start) & (rets.index <= self.end)].mean(axis=1, skipna=True).fillna(0.0)
            eq = (1 + rets).cumprod() * self.bt.initial_capital
            def sharpe(r):
                sd = r.std(ddof=1)
                return float(r.mean() / sd * np.sqrt(self.bt.bars_per_year)) if sd > 0 else 0.0
            blocks = [rets.iloc[b] for b in np.array_split(np.arange(len(rets)), self.cfg.blocks)]
            yearly = periodic_returns(eq, "YE")
            self._passive = _clean({
                "rules": f"Mantener las {len(self.research)} acciones a partes iguales, sin hacer nada",
                "consistency": min(sharpe(b) for b in blocks), "sharpe": sharpe(rets),
                "cagr": float((eq.iloc[-1] / eq.iloc[0]) ** (self.bt.bars_per_year / max(len(eq) - 1, 1)) - 1),
                "max_drawdown": float((eq / eq.cummax() - 1).min()), "pct_positive_years": float((yearly > 0).mean()),
                "worst_year": float(yearly.min()),
                "blocks": [{"start": str(b.index[0].date()), "end": str(b.index[-1].date()), "sharpe": sharpe(b),
                            "return": float((1 + b).prod() - 1)} for b in blocks]})
        return self._passive

    def oos_benchmarks(self) -> dict:
        """Buy & hold references over the OOS period (same stocks equal weight; benchmark). Only used to judge a
        final test; never shown to the search or the AI."""
        if getattr(self, "_oos_bench", None) is None:
            oos = pd.Timestamp(self.cfg.oos_start, tz="UTC")
            end = max(v.index.max() for v in self.full.values())
            rets = pd.concat({k: v["close"].pct_change() for k, v in self.full.items()}, axis=1)
            rets = rets[(rets.index >= oos) & (rets.index <= end)].mean(axis=1, skipna=True)
            out = {"passive": window_stats(rets, self.bt.bars_per_year)}
            if self.benchmark is not None:
                b = self.benchmark.pct_change()
                out["benchmark"] = window_stats(b[(b.index >= oos) & (b.index <= end)], self.bt.bars_per_year)
            self._oos_bench = _clean(out)
        return self._oos_bench

    def _judge(self, row, oos: dict) -> dict:
        bench = self.oos_benchmarks()
        research_sharpe = ((row.validation or {}).get("is_metrics") or {}).get("sharpe") or (row.metrics or {}).get("sharpe")
        v = final_verdict(oos, research_sharpe, bench.get("passive"))
        return {**v, "research_sharpe": research_sharpe, "passive": bench.get("passive"),
                "benchmark": bench.get("benchmark"), "criteria_version": FINAL_CRITERIA_VERSION}

    def rejudge_finals(self) -> int:
        """Final tests judged under the old, too lenient rule (only 'made money') are re-judged with the current
        criteria from their STORED out-of-sample metrics (the vault is not opened again)."""
        R = m.ResearchCandidate
        with self.sf() as s:
            rows = s.scalars(select(R).where(R.universe_id == self.universe_id,
                                             R.status.in_(("FINAL_PASS", "FINAL_FAIL")))).all()
        changed = 0
        for row in rows:
            fin = dict(row.final or {})
            if fin.get("criteria_version") == FINAL_CRITERIA_VERSION or not fin.get("oos"):
                continue
            j = self._judge(row, fin["oos"])
            decision = "FINAL_PASS" if j["passed"] else "FINAL_FAIL"
            fin.update(_clean(j))
            if decision != fin.get("decision"):
                fin["rejudged"] = f"{fin.get('decision')} -> {decision} (criterios del test final endurecidos)"
                changed += 1
                self._demote(row.strategy_id)
            fin["decision"] = decision
            with self.sf() as s, s.begin():
                r = s.get(R, row.id)
                r.final, r.status = fin, decision
        if changed:
            self.log(f"{changed} estrategia(s) aprobadas con el criterio antiguo ya no pasan el test final")
        return changed

    def _demote(self, sid: str | None) -> None:
        if not sid:
            return
        try:
            st = self.registry.status(sid)
        except Exception:  # noqa: BLE001
            return
        if st in (Status.CANDIDATE, Status.PAPER, Status.UNDER_REVIEW):
            with self.sf() as s, s.begin():
                for ps in s.scalars(select(m.PaperSession).where(m.PaperSession.strategy_id == sid,
                                                                 m.PaperSession.status == "ACTIVE")).all():
                    ps.status, ps.stop_reason = "STOPPED", "suspende el test final con los criterios corregidos"
                    ps.stopped_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self.registry.transition(sid, Status.REJECTED, actor="system",
                                     reason="final OOS test re-judged: does not beat holding the same stocks / decays")

    def _row_id(self, vid: str) -> str:
        return hash_obj({"version": vid, "universe": self.universe_id}, 32)

    def get_row(self, row_id: str) -> m.ResearchCandidate:
        with self.sf() as s:
            row = s.get(m.ResearchCandidate, row_id)
        if row is None:
            raise KeyError(row_id)
        if row.universe_id != self.universe_id:
            raise ValueError("esta estrategia se evaluó con otro conjunto de acciones; vuelve a investigar con los datos actuales")
        return row

    # -------------------------------------------------------------- bookkeeping
    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRequested()

    def evaluate_and_store(self, sd: StrategyDefinition, origin: str, cycle: int) -> tuple[float | None, dict, bool]:
        """Score a candidate once; returns (fitness, metrics, is_new_trial). Known versions are not re-counted."""
        vid = sd.version_id
        R = m.ResearchCandidate
        q = select(R).where(R.version_id == vid, R.universe_id == self.universe_id)
        with self.sf() as s:
            row = s.scalars(q).first()
            if row is not None:
                return row.fitness, row.metrics or {}, False
        err = None
        try:
            fit, met = self.evaluator.evaluate(sd)
        except Exception as e:  # noqa: BLE001 - a broken candidate is a failed trial, never a crash
            fit, met, err = None, {}, repr(e)[:500]
        status = "EVALUATED" if fit is not None else "INVALID"
        with self.sf() as s, s.begin():
            if s.scalars(q).first() is None:
                sr = met.get("sr")
                s.add(m.ResearchCandidate(id=self._row_id(vid), version_id=vid, universe_id=self.universe_id,
                                          origin=origin, cycle=cycle, definition=sd.to_dict(), fitness=fit,
                                          sr=sr if isinstance(sr, (int, float)) else None,
                                          metrics=met, status=status, dataset_id=self.dataset_id,
                                          error=err or met.get("invalid_reason")))
        self.session_trials += 1
        return fit, met, True

    def trial_stats(self) -> tuple[int, float]:
        """(number of trials ever run, variance of their per-period Sharpe) for the Deflated Sharpe Ratio.
        Counted over ALL universes: conservative, since earlier searches shaped what is tried next."""
        R = m.ResearchCandidate
        with self.sf() as s:
            n = s.scalar(select(func.count()).select_from(R)) or 0
            k, mean, mean2 = s.execute(select(func.count(R.sr), func.avg(R.sr), func.avg(R.sr * R.sr))).one()
        var = float((mean2 - mean ** 2) * k / (k - 1)) if k and k > 1 else 1.0 / max(len(self.index), 1)
        return int(n), max(var, 1e-12)

    def _top_rows(self, n: int, origin: str | None = None, statuses: tuple[str, ...] | None = None) -> list:
        with self.sf() as s:
            q = select(m.ResearchCandidate).where(m.ResearchCandidate.fitness.is_not(None),
                                                  m.ResearchCandidate.universe_id == self.universe_id)
            if origin:
                q = q.where(m.ResearchCandidate.origin == origin)
            if statuses:
                q = q.where(m.ResearchCandidate.status.in_(statuses))
            return list(s.scalars(q.order_by(m.ResearchCandidate.fitness.desc()).limit(n)))

    def next_cycle(self) -> int:
        with self.sf() as s:
            return int(s.scalar(select(func.max(m.ResearchCandidate.cycle))) or 0) + 1

    # -------------------------------------------------------------- one cycle
    def engine(self) -> _ConsistencyEvolution:
        if self._engine is None:
            self.phase = "preparando datos"
            self._engine = _ConsistencyEvolution(
                self, self.research, (self.start, self.end), self.bt,
                EvolutionConfig(population=self.cfg.population, generations=self.cfg.generations, seed=self.cfg.seed,
                                init_hold_choices=self._holds(), hold_choices=self._holds(),
                                complexity_penalty=self.cfg.complexity_penalty))
        return self._engine

    def _holds(self) -> tuple:
        return tuple(h for h in (2, 3, 5, 7, 10, 15, 20) if h <= self.cfg.max_holding_days) or (self.cfg.max_holding_days,)

    def holding_ok(self, sd: StrategyDefinition) -> bool:
        if sd.max_holding_bars is None:
            return False
        try:
            return 1 <= int(sd.resolve(sd.max_holding_bars)) <= self.cfg.max_holding_days
        except (KeyError, TypeError, ValueError):
            return False

    def seed_baselines(self) -> None:
        for sd in (momentum_baseline(), trend_baseline()):
            self.evaluate_and_store(sd, "baseline", 0)

    def import_previous(self, n: int = 30) -> int:
        """A new ranking (new data or rules) starts from what earlier rankings learnt: their best strategies are
        re-scored here under the current rules (each re-score is a counted trial)."""
        if self._imported:
            return 0
        self._imported = True
        R = m.ResearchCandidate
        with self.sf() as s:
            if s.scalar(select(func.count()).select_from(R).where(R.universe_id == self.universe_id,
                                                                  R.origin != "baseline")) >= n:
                return 0
            prev = s.scalars(select(R).where(R.universe_id != self.universe_id, R.fitness.is_not(None),
                                            R.origin.in_(("evolution", "ai"))).order_by(R.fitness.desc()).limit(n * 4)).all()
        seen, picked = set(), []
        for r in prev:
            vid = r.version_id or r.id
            if vid not in seen:
                seen.add(vid)
                picked.append(r)
            if len(picked) >= n:
                break
        picked = [r for r in picked if self.holding_ok(definition_from_dict(r.definition))]
        for r in picked:
            self.check_stop()
            self.evaluate_and_store(definition_from_dict(r.definition), r.origin, 0)
        if picked:
            self.log(f"Reaprovechando las {len(picked)} mejores estrategias de investigaciones anteriores "
                     "(se vuelven a puntuar con las reglas y datos actuales)")
        return len(picked)

    def run_cycle(self, cycle: int) -> dict:
        before = self.session_trials
        self.seed_baselines()
        self.phase = "reaprovechando investigación anterior"
        self.import_previous()
        self.phase = "búsqueda evolutiva"
        eng = self.engine()
        eng.cycle = cycle
        eng.rng = np.random.default_rng([self.cfg.seed, cycle])
        elites = [definition_from_dict(r.definition) for r in self._top_rows(self.cfg.elites_from_history, "evolution")]
        self.log(f"Ciclo {cycle}: evolución ({self.cfg.population}×{self.cfg.generations}) partiendo de {len(elites)} mejores anteriores")
        eng.run(initial=elites)
        if self.cfg.use_ai and self.ai is not None:
            self.phase = "IA proponiendo ideas"
            self._ai_round(cycle)
        self.phase = "validando finalistas"
        validated = self._validate_finalists()
        best = self._top_rows(1)
        out = {"cycle": cycle, "new_trials": self.session_trials - before, "validated": validated,
               "best_fitness": best[0].fitness if best else None}
        self.log(f"Ciclo {cycle} terminado: {out['new_trials']} estrategias nuevas probadas; "
                 f"mejor consistencia {_num(out['best_fitness']) if out['best_fitness'] is not None else '—'}")
        return out

    def run(self, max_cycles: int = 0, on_cycle: Callable[[dict], None] | None = None) -> int:
        done, cycle = 0, self.next_cycle()
        # indicators repeat across candidates: cache them (exact-content keys, bit-identical results)
        with feature_cache(max_items=min(12000, 60 * max(len(self.research), 1))):
            while not self.stop_event.is_set() and (max_cycles <= 0 or done < max_cycles):
                try:
                    summary = self.run_cycle(cycle)
                except StopRequested:
                    self.log("Detenido por el usuario")
                    break
                if on_cycle:
                    on_cycle(summary)
                done, cycle = done + 1, cycle + 1
        self.phase = "parado"
        return done

    # -------------------------------------------------------------- AI
    def ai_context(self) -> dict:
        """What the AI may see: research-period results only. Final-test (OOS) results are never included."""
        n, _ = self.trial_stats()
        best = []
        for r in self._top_rows(6):
            sd = definition_from_dict(r.definition)
            mt = r.metrics or {}
            best.append({"rules": describe(sd), "origin": r.origin, "consistency_score": round(r.fitness, 3),
                         "sharpe": mt.get("sharpe"), "block_sharpes": [b["sharpe"] for b in mt.get("blocks", [])],
                         "pct_positive_years": mt.get("pct_positive_years"), "n_trades": mt.get("n_trades")})
        with self.sf() as s:
            reasons = s.scalars(select(m.ResearchCandidate.error).where(m.ResearchCandidate.status == "INVALID",
                                                                         m.ResearchCandidate.universe_id == self.universe_id)
                                .order_by(m.ResearchCandidate.created_at.desc()).limit(200)).all()
        failures: dict[str, int] = {}
        for x in reasons:
            key = (x or "error").split("(")[0].strip()[:60]
            failures[key] = failures.get(key, 0) + 1
        return {"goal": ("Find LONG-ONLY daily swing-trading rules for these US large caps that are CONSISTENT: the "
                         "score is the WORST annualised Sharpe across 3 consecutive sub-periods of the research window, "
                         "minus 0.03 per complexity point. Few rules, few parameters, at least 30 trades. Every trade "
                         f"must close within {self.cfg.max_holding_days} trading days."),
                "universe": sorted(self.research), "research_period": [str(self.start.date()), str(self.end.date())],
                "best_so_far": best, "recent_failure_reasons": failures, "trials_so_far": n,
                "passive_benchmark_to_beat": {k: self.passive_reference()[k] for k in ("consistency", "sharpe")},
                "your_last_rejected_proposals": self._last_ai_rejections[-5:],
                "event_features": ("days_since_earnings, earnings_surprise (percent) and days_to_earnings (sessions; "
                                   f"{self.bt.earnings_blackout_days and 'entries within ' + str(self.bt.earnings_blackout_days) + ' sessions of results are blocked by the engine' or 'no blackout'}) "
                                   "are available when earnings data exists"),
                "holding_period": f"swing trading: max_holding_bars is REQUIRED, an integer from 1 to "
                                  f"{self.cfg.max_holding_days} (trading days), 1-10 preferred; exits may come earlier "
                                  "via stop/target/exit rules",
                "rules": "direction must be 'long'; operands are objects like {\"feature\": \"rsi\", \"params\": {\"n\": 14}}"
                         " or {\"value\": 30}; never a bare string."}

    def _ai_round(self, cycle: int) -> None:
        try:
            out = self.ai.propose_strategies(self.ai_context(), n=self.cfg.ai_proposals, dataset_version=self.dataset_id)
        except AIBudgetExceeded as e:
            self.log(f"IA: presupuesto diario agotado ({e}); sigo sin IA")
            return
        except (AIProviderError, AIOutputRejected) as e:
            self.log(f"IA: sin propuestas válidas ({e})")
            return
        self._last_ai_rejections = [f"{(raw or {}).get('name')}: {why}" for raw, why in out["rejected"]]
        for raw, why in out["rejected"]:
            self.log(f"IA: propuesta rechazada por formato ({(raw or {}).get('name')}: {why[:80]})")
        for sd in out["accepted"]:
            self.check_stop()
            if sd.direction != "long":
                self.log(f"IA: '{sd.name}' descartada (solo se admiten estrategias de compra)")
                continue
            if not self.holding_ok(sd):
                self._last_ai_rejections.append(f"{sd.name}: max_holding_bars must be 1..{self.cfg.max_holding_days}")
                self.log(f"IA: '{sd.name}' descartada (no cierra en {self.cfg.max_holding_days} días como máximo)")
                continue
            fit, met, new = self.evaluate_and_store(sd, "ai", cycle)
            self.log(f"IA: '{sd.name}' → " + (f"consistencia {fit:.3f}" if fit is not None else
                                              f"no puntuable ({met.get('invalid_reason', 'error')})"))

    # -------------------------------------------------------------- validation (research data only)
    def _validate_finalists(self) -> int:
        pool = self._top_rows(self.cfg.finalist_pool)
        stale = [r for r in pool if r.status == "VALIDATED_PASS" and "beats_passive" not in ((r.validation or {}).get("gates") or {})]
        todo = (stale + [r for r in pool if r.status == "EVALUATED" and r.origin != "baseline"])[: self.cfg.finalists_per_cycle]
        for r in todo:
            self.check_stop()
            self.validate(r.id)
        return len(todo)

    def validate(self, vid: str) -> dict:
        row = self.get_row(vid)
        sd = definition_from_dict(row.definition)
        self.log(f"Validando {vid[:8]}: {describe(sd)[:90]}")
        n_trials, var_sr = self.trial_stats()
        res, mt = run_window(sd, self.research, self.bt, self.start, self.end)
        folds = make_folds(self.index, self.cfg.wf_train, self.cfg.wf_validate, self.cfg.wf_test, self.cfg.embargo)
        wf = walk_forward(sd, self.research, {k: [v] for k, v in sd.params.items()}, folds, self.bt) if folds else None
        rob = parameter_robustness(sd, self.research, self.bt, self.start, self.end) if sd.params else None
        mc = monte_carlo_trades(res.trades, self.bt.initial_capital, self.cfg.mc_sims, self.cfg.seed)
        costs = cost_sensitivity(sd, self.research, self.bt, self.start, self.end, multipliers=(1.0, 2.0))
        base = {b.name: run_window(b, self.research, self.bt, self.start, self.end)[1].get("sharpe")
                for b in (momentum_baseline(), trend_baseline())}
        ovf = overfitting_risk(metrics=mt, complexity=sd.complexity(), trades=res.trades, returns=res.returns,
                               n_trials=max(n_trials, 1), var_trial_sr=var_sr,
                               wf_efficiency=wf.summary.get("wf_efficiency") if wf else None,
                               param_stability=rob.stability if rob else None)
        sc = strategy_score(is_metrics=mt, oos_metrics=None, wf_summary=wf.summary if wf else None,
                            robustness_stability=rob.stability if rob else None,
                            mc=mc if "total_return" in mc else None, overfit=ovf, complexity=sd.complexity(),
                            monthly_returns=periodic_returns(res.equity["equity"], "ME"), baseline_sharpes=base,
                            cfg=ScoreConfig(require_oos_positive=False))
        gates = dict(sc["gates"])
        gates["robustness"] = rob is None or bool(rob.passed)
        s2 = next((c.get("sharpe") for c in costs if c.get("cost_multiplier") == 2.0), None)
        gates["costs_2x"] = s2 is not None and np.isfinite(s2) and s2 > 0
        pas = self.passive_reference()
        gates["beats_passive"] = (row.fitness is not None and row.fitness > pas["consistency"]
                                  and (mt.get("sharpe") or -np.inf) > pas["sharpe"])
        gates = {k: bool(v) for k, v in gates.items()}
        passed = all(gates.values())
        sid = f"auto-{row.origin}-{(row.version_id or vid)[:8]}-{self.universe_id[:4]}"
        self.registry.register(sid, sd, origin=row.origin)
        rec = self.tracker.run_backtest(sd, self.research, self.bt, start=self.start, end=self.end, seed=self.cfg.seed,
                                        strategy_id=sid, kind="autoresearch")
        failed_txt = f"research validation failed: {sorted(k for k, v in gates.items() if not v)}"
        cur = self.registry.status(sid)
        if cur is Status.REJECTED and passed:  # re-validated under new rules: reopen through RESEARCH (history kept)
            self.registry.transition(sid, Status.RESEARCH, reason="re-validation", actor="system")
            cur = Status.RESEARCH
        if cur is Status.RESEARCH:
            self.registry.transition(sid, Status.BACKTESTED, reason="autoresearch consistency backtest", actor="system",
                                     evidence={"backtest_experiment_id": rec.id})
            cur = Status.BACKTESTED
        if cur is Status.BACKTESTED:
            self.registry.transition(sid, Status.VALIDATING if passed else Status.REJECTED, actor="system",
                                     reason="research validation passed" if passed else failed_txt)
        elif cur is Status.VALIDATING and not passed:
            self.registry.transition(sid, Status.REJECTED, actor="system", reason=failed_txt)
        val = _clean({"passed": passed, "gates": gates, "failed": sorted(k for k, v in gates.items() if not v),
                      "experiment_id": rec.id, "n_trials_at_validation": n_trials, "passive": pas,
                      "walk_forward": wf.summary if wf else None,
                      "robustness": None if rob is None else {"stability": rob.stability, "passed": rob.passed,
                                                              "peak_sharpness": rob.peak_sharpness},
                      "monte_carlo": {k: v for k, v in mc.items() if k != "paths"}, "costs": costs, "baselines": base,
                      "overfitting": ovf, "score": sc["score"], "is_metrics": {k: mt.get(k) for k in
                                                                              ("sharpe", "cagr", "max_drawdown", "n_trades")}})
        with self.sf() as s, s.begin():
            r = s.get(m.ResearchCandidate, vid)
            r.validation, r.status, r.strategy_id = val, "VALIDATED_PASS" if passed else "VALIDATED_FAIL", sid
        self.log(f"Validación {vid[:8]}: " + ("PASA (lista para el test final)" if passed else f"NO pasa ({', '.join(val['failed'])})"))
        return val

    # -------------------------------------------------------------- the one-time OOS test (manual)
    def final_test(self, vid: str) -> dict:
        """Opens the OOS vault for this version (once, logged). Only for candidates that passed validation."""
        row = self.get_row(vid)
        if row.status == "VALIDATED_PASS" and "beats_passive" not in ((row.validation or {}).get("gates") or {}):
            self.validate(vid)  # validated under older rules: re-check before spending the one-time test
            row = self.get_row(vid)
        if row.status != "VALIDATED_PASS":
            raise ValueError("solo se puede hacer el test final a estrategias que pasaron la validación")
        sd = definition_from_dict(row.definition)
        oos_full = self.vault.evaluate(sd, self.full, self.bt, purpose=f"autoresearch final test {vid}")
        oos = {k: v for k, v in oos_full.items() if not k.startswith("_")}
        val = row.validation or {}
        judged = self._judge(row, oos)
        ok = judged["passed"]
        decision = "FINAL_PASS" if ok else "FINAL_FAIL"
        sid = row.strategy_id
        if ok:
            exp = val.get("experiment_id")
            self.registry.transition(sid, Status.CANDIDATE, actor="user", reason="autoresearch final OOS test passed",
                                     evidence={"walk_forward_experiment_id": f"{exp}#walk_forward",
                                               "robustness_passed": (val.get("robustness") or {}).get("passed", "n/a: no params"),
                                               "oos_experiment_id": f"oos:{vid}", "monte_carlo_experiment_id": f"{exp}#monte_carlo"})
        else:
            self.registry.transition(sid, Status.REJECTED, actor="user", reason="autoresearch final OOS test failed")
        final = _clean({"oos": {k: oos.get(k) for k in ("total_return", "cagr", "sharpe", "max_drawdown", "n_trades",
                                                        "win_rate", "profit_factor")},
                        "period": [self.cfg.oos_start, str(max(v.index.max() for v in self.full.values()).date())],
                        "decision": decision, "at": datetime.now(timezone.utc).isoformat(), **judged})
        with self.sf() as s, s.begin():
            r = s.get(m.ResearchCandidate, vid)
            r.final, r.status = final, decision
        self.log(f"Test final {vid[:8]}: {'APROBADA → CANDIDATE' if ok else 'SUSPENDE → REJECTED'}")
        return final

    # -------------------------------------------------------------- backtest view (UI)
    def backtest_view(self, vid: str, max_trades: int = 400) -> dict:
        """Backtest of one ranked strategy vs buy & hold of the benchmark over the RESEARCH period. Only after its
        final test (the vault is already open for this version) does it extend into the OOS period."""
        row = self.get_row(vid)
        sd = definition_from_dict(row.definition)
        with_oos = row.status in ("FINAL_PASS", "FINAL_FAIL")
        data = self.full if with_oos else self.research
        end = max(v.index.max() for v in data.values()) if with_oos else self.end
        res, mt = run_window(sd, data, self.bt, self.start, end)
        eq = res.equity["equity"]
        out = {"id": vid, "rules": describe(sd), "period": [str(self.start.date()), str(pd.Timestamp(end).date())],
               "includes_oos": with_oos, "oos_start": self.cfg.oos_start,
               "metrics": {k: mt.get(k) for k in ("total_return", "cagr", "sharpe", "max_drawdown", "volatility",
                                                  "n_trades", "win_rate", "profit_factor", "exposure", "avg_trade_bars")},
               "equity": [{"time": int(t.timestamp()), "value": float(v)} for t, v in eq.items()]}
        yearly = periodic_returns(eq, "YE")
        bench_yearly = None
        if self.benchmark is not None:
            from qsts.backtest.benchmarks import buy_and_hold
            from qsts.backtest.metrics import compute_metrics
            b = self.benchmark[(self.benchmark.index >= self.start) & (self.benchmark.index <= end)]
            if len(b) > 1:
                beq = buy_and_hold(b, self.bt)["equity"]
                bm = compute_metrics(pd.DataFrame({"equity": beq, "gross_exposure": 1.0}), pd.DataFrame())
                out["benchmark"] = {"metrics": {k: bm.get(k) for k in ("total_return", "cagr", "sharpe", "max_drawdown",
                                                                         "volatility")},
                                    "equity": [{"time": int(t.timestamp()), "value": float(v)} for t, v in beq.items()]}
                bench_yearly = periodic_returns(beq, "YE")
        out["yearly"] = [{"year": int(k.year), "strategy": float(v),
                          "benchmark": float(bench_yearly.get(k)) if bench_yearly is not None and k in bench_yearly else None}
                         for k, v in yearly.items()]
        tr = res.trades
        if len(tr):
            tr = tr.sort_values("entry_ts").tail(max_trades)
            out["trades"] = [{"symbol": t.symbol, "entry": str(t.entry_ts)[:10], "exit": str(t.exit_ts)[:10],
                              "entry_price": float(t.entry_price), "exit_price": float(t.exit_price),
                              "pnl": float(t.pnl), "r": float(t.r_multiple), "reason": t.exit_reason}
                             for t in tr.itertuples(index=False)]
        else:
            out["trades"] = []
        return _clean(out)

    # -------------------------------------------------------------- leaderboard
    def leaderboard(self, limit: int = 20) -> dict:
        n, var = self.trial_stats()
        sr0 = expected_max_sharpe(max(n, 1), var)
        with self.sf() as s:
            finals = s.scalar(select(func.count()).select_from(m.OOSAccessLog)) or 0
        rows, seen, hidden = [], set(), 0
        for r in self._top_rows(limit * 4):
            mt = r.metrics or {}
            # logically equivalent rule sets (e.g. a redundant extra condition) trade identically: show the
            # best-ranked one only (the complexity penalty already ranks the simpler one first)
            sig = (mt.get("n_trades"), round(mt.get("sharpe") or 0, 9), round(mt.get("max_drawdown") or 0, 9))
            if sig in seen:
                hidden += 1
                continue
            seen.add(sig)
            if len(rows) >= limit:
                continue
            sd = definition_from_dict(r.definition)
            dsr = psr_from_stats(mt["sr"], mt["skew"], mt["kurt"], mt["T"], sr0) if mt.get("T") else None
            rows.append({"id": r.id, "origin": r.origin, "cycle": r.cycle, "rules": describe(sd), "name": sd.name,
                         "consistency": r.fitness, "sharpe": mt.get("sharpe"), "cagr": mt.get("cagr"),
                         "max_drawdown": mt.get("max_drawdown"), "n_trades": mt.get("n_trades"),
                         "pct_positive_years": mt.get("pct_positive_years"), "worst_year": mt.get("worst_year"),
                         "avg_days": mt.get("avg_trade_bars"),
                         "blocks": mt.get("blocks"), "dsr": dsr, "status": r.status, "strategy_id": r.strategy_id,
                         "validation": r.validation, "final": r.final})
        R = m.ResearchCandidate
        with self.sf() as s:
            by_status = dict(s.execute(select(R.status, func.count()).where(R.universe_id == self.universe_id)
                                       .group_by(R.status)).all())
        return _clean({"n_trials": n, "n_trials_universe": int(sum(by_status.values())), "final_tests_used": finals,
                       "universe": {"id": self.universe_id, "n_symbols": len(self.research),
                                    "with_earnings": sum("earn_days_to" in v.columns for v in self.research.values())},
                       "earnings_rule": {"blackout_days": self.bt.earnings_blackout_days,
                                         "exit_before": self.bt.exit_before_earnings},
                       "passive": self.passive_reference(),
                       "by_status": by_status, "equivalents_hidden": hidden,
                       "research_period": [str(self.start.date()), str(self.end.date())], "oos_start": self.cfg.oos_start,
                       "rows": rows})


# ---------------------------------------------------------------------- background runner (UI)
@dataclass
class RunnerState:
    running: bool = False
    phase: str = "parado"
    started_at: str | None = None
    cycles_done: int = 0
    session_trials: int = 0
    error: str | None = None
    config: dict = field(default_factory=dict)
    history: list = field(default_factory=list)


class AutoResearchRunner:
    """At most one research loop at a time, in a daemon thread, so the UI stays usable."""

    def __init__(self, build: Callable[[AutoResearchConfig, Callable[[str], None], threading.Event], AutoResearcher]):
        self._build = build
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = RunnerState()
        self.logs: deque[str] = deque(maxlen=300)
        self.researcher: AutoResearcher | None = None

    def log(self, msg: str) -> None:
        self.logs.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    def start(self, cfg: AutoResearchConfig, max_cycles: int = 0) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop = threading.Event()
            self.state = RunnerState(running=True, phase="arrancando",
                                     started_at=datetime.now(timezone.utc).isoformat(), config=asdict(cfg))
            self._thread = threading.Thread(target=self._run, args=(cfg, max_cycles), daemon=True, name="autoresearch")
            self._thread.start()
            return True

    def _run(self, cfg: AutoResearchConfig, max_cycles: int) -> None:
        keep_awake(True)  # a day-long run must not be paused by Windows sleep
        try:
            self.log("Cargando datos (solo el periodo de investigación)…")
            self.researcher = self._build(cfg, self.log, self._stop)

            def on_cycle(summary):
                self.state.cycles_done += 1
                self.state.history.append(summary)
                self.state.session_trials = self.researcher.session_trials
            self.researcher.run(max_cycles, on_cycle)
        except Exception as e:  # noqa: BLE001
            self.state.error = repr(e)[:500]
            self.log(f"ERROR: {e!r}")
        finally:
            keep_awake(False)
            self.state.running = False
            self.state.phase = "parado"

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def status(self) -> dict:
        st = asdict(self.state)
        if self.researcher is not None:
            st["phase"] = self.researcher.phase if self.state.running else "parado"
            st["session_trials"] = self.researcher.session_trials
        st["log"] = list(self.logs)[-60:]
        return st
