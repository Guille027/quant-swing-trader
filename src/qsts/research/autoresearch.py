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
from qsts.backtest.engine import ENGINE_VERSION, BacktestConfig, signal_cache
from qsts.backtest.metrics import periodic_returns
from qsts.core.hashing import hash_obj
from qsts.core.power import keep_awake
from qsts.db import models as m
from qsts.features.registry import REGISTRY, feature_cache
from qsts.research.evolution import EvolutionConfig, EvolutionEngine, Individual, idea
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
    complexity_penalty: float = 0.05  # Sharpe points per complexity point (params + rules + indicators)
    max_conditions: int = 2           # entry conditions per strategy (fewer = less room to memorise the past)
    split_halves: bool = True         # the score must hold on two random halves of the stocks, separately
    split_seed: int = 7
    holdout_years: float = 2.0        # "examen previo": last years of the research period, never seen by the search
    fresh_start: bool = False         # explore from zero: no seeds / imports from earlier work (rejoin them later)
    max_holding_days: int = 20      # swing trading: every position is closed after at most this many sessions
    population: int = 20
    generations: int = 4
    elites_from_history: int = 6
    elites_per_idea: int = 2         # earlier best strategies that seed a cycle: at most this many per indicator idea
    immigrants: float = 0.2          # share of each generation made of brand-new random strategies
    max_feature_share: float = 0.5   # no indicator in more than half of the population
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


def universe_changes(old: dict | None, new: dict) -> list[str] | None:
    """Plain-language reasons why two rankings differ (None if the old ranking's inputs were not recorded)."""
    if old is None:
        return None
    out = []
    so, sn = old.get("symbols") or {}, new.get("symbols") or {}
    added, removed = set(sn) - set(so), set(so) - set(sn)
    moved = [k for k in set(so) & set(sn) if so[k] != sn[k]]
    n = lambda k, one, many: f"{k} {one if k == 1 else many}"  # noqa: E731
    if added:
        out.append(n(len(added), "acción nueva", "acciones nuevas"))
    if removed:
        out.append(n(len(removed), "acción menos", "acciones menos"))
    if moved:
        out.append(n(len(moved), "acción cuyo historial empieza en otra fecha", "acciones cuyo historial empieza en otra fecha"))
    if ("earnings_data" in old) != ("earnings_data" in new):
        out.append("la casilla 'evitar resultados trimestrales' está " + ("marcada" if "earnings_data" in new else "desmarcada"))
    elif old.get("earnings_data") != new.get("earnings_data"):
        out.append("datos de resultados trimestrales distintos (por ejemplo, recién descargados)")
    off = ("earnings_blackout_days", "exit_before_earnings")
    eng = lambda k: {a: b for a, b in (k.get("engine") or {}).items() if a not in off}  # noqa: E731
    if eng(old) != eng(new) or old.get("engine_version") != new.get("engine_version"):
        out.append("una versión nueva del simulador")
    if any(old.get(k) != new.get(k) for k in ("oos_start", "warmup", "blocks", "min_trades", "min_block_trades",
                                              "complexity_penalty")):
        out.append("reglas de puntuación distintas")
    return out


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
def split_universe(symbols, seed: int = 7) -> list[list[str]]:
    """Two random halves of the stocks (fixed seed: same halves for every strategy of a ranking)."""
    syms = sorted(symbols)
    rng = np.random.default_rng(seed)
    order = [syms[i] for i in rng.permutation(len(syms))]
    return [sorted(order[: len(order) // 2]), sorted(order[len(order) // 2:])]


def _blocks(res, n_blocks: int, bars_per_year: int) -> list[dict]:
    rets = res.equity["equity"].pct_change().dropna()
    tr = res.trades
    entries = pd.DatetimeIndex(tr["entry_ts"]) if len(tr) else pd.DatetimeIndex([], tz="UTC")
    out = []
    for b in np.array_split(np.arange(len(rets)), n_blocks):
        r = rets.iloc[b]
        sdev = r.std(ddof=1)
        out.append({"start": str(r.index[0].date()), "end": str(r.index[-1].date()),
                    "sharpe": float(r.mean() / sdev * np.sqrt(bars_per_year)) if sdev > 0 else 0.0,
                    "return": float((1 + r).prod() - 1),
                    "trades": int(((entries >= r.index[0]) & (entries <= r.index[-1])).sum())})
    return out


class ConsistencyEvaluator:
    """One backtest over the SEARCH window cut into consecutive blocks; with `halves`, the same strategy is also
    run on each half of the stocks separately and must hold in every block of both (a rule that only fits some
    stocks fails here). The score is the worst block Sharpe of all of them minus the complexity penalty."""

    def __init__(self, research: dict[str, pd.DataFrame], start, end, bt_cfg: BacktestConfig, cfg: AutoResearchConfig,
                 halves: list[list[str]] | None = None):
        self.data, self.start, self.end, self.bt, self.cfg = research, start, end, bt_cfg, cfg
        self.halves = [h for h in (halves or []) if h]

    def evaluate(self, sd: StrategyDefinition) -> tuple[float | None, dict]:
        with signal_cache(max_items=2 * len(self.data) + 10):  # the halves reuse the signals of the full run
            res, mt = run_window(sd, self.data, self.bt, self.start, self.end)
            parts = [run_window(sd, {k: self.data[k] for k in h}, self.bt, self.start, self.end)
                     for h in self.halves]
        eq = res.equity["equity"]
        rets = eq.pct_change().dropna()
        blocks = _blocks(res, self.cfg.blocks, self.bt.bars_per_year)
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
        worst = [b["sharpe"] for b in blocks]
        halves = []
        for name, (pres, pmt) in zip("AB", parts):
            pb = _blocks(pres, self.cfg.blocks, self.bt.bars_per_year)
            halves.append({"name": name, "consistency": min(b["sharpe"] for b in pb), "sharpe": pmt.get("sharpe"),
                           "n_trades": pmt.get("n_trades", 0), "blocks": pb})
            low = [b for b in pb if b["trades"] < max(2, self.cfg.min_block_trades // 2)]
            if low:
                metrics["halves"] = halves
                metrics["invalid_reason"] = f"casi sin operaciones con la mitad {name} de las acciones en {low[0]['start']}"
                return None, _clean(metrics)
            worst += [b["sharpe"] for b in pb]
        if halves:
            metrics["halves"] = halves
        fit = min(worst) - self.cfg.complexity_penalty * cx
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
            raise ValueError("aún no hay datos suficientes para investigar: descarga acciones en 1 · Datos "
                             "(o carga la copia de otro ordenador en Inicio)")
        self.index = idx[cfg.warmup_bars:]
        self.start, self.end = self.index[0], self.index[-1]
        # the search only sees [start, search_end]; the last `holdout_years` are a pre-exam it never optimises on
        self.search_end, self.holdout_start = self.end, None
        if cfg.holdout_years > 0:
            cut = self.end - pd.DateOffset(years=cfg.holdout_years)
            before = self.index[self.index <= cut]
            if len(before) >= 2 * 252:
                self.search_end, self.holdout_start = before[-1], self.index[self.index > cut][0]
        self.search_index = self.index[self.index <= self.search_end]
        self.halves = split_universe(self.research, cfg.split_seed) if cfg.split_halves and len(self.research) >= 4 else []
        self.evaluator = ConsistencyEvaluator(self.research, self.start, self.search_end, bt_cfg, cfg, self.halves)
        self.dataset_id = hash_obj(dataset_fingerprint(self.research), 32)
        self.benchmark = benchmark
        # a ranking only compares strategies scored on the same symbols, window and scoring rules
        key = {"symbols": {k: str(v.index[0].date()) for k, v in sorted(self.research.items())},
               "oos_start": cfg.oos_start, "warmup": cfg.warmup_bars, "blocks": cfg.blocks,
               "min_trades": cfg.min_trades, "min_block_trades": cfg.min_block_trades,
               "complexity_penalty": cfg.complexity_penalty, "engine": _engine_key(bt_cfg),
               "engine_version": ENGINE_VERSION}
        if self.halves:
            key["split_halves"] = cfg.split_seed
        if cfg.max_holding_days != 20:  # another horizon / complexity = another ranking (defaults keep the old one)
            key["max_holding_days"] = cfg.max_holding_days
        if cfg.max_conditions != 2:
            key["max_conditions"] = cfg.max_conditions
        if self.holdout_start is not None:
            key["holdout_start"] = str(self.holdout_start.date())
        if bt_cfg.earnings_blackout_days or bt_cfg.exit_before_earnings:
            # the earnings rules make scores depend on the earnings data; with the rules off, strategies that do not
            # use earnings features score identically, so earlier rankings stay valid
            key["earnings_data"] = _earnings_key(self.research)
        self.universe_id, self.universe_key = hash_obj(key, 32), _clean(key)
        self.registry, self.tracker = StrategyRegistry(sf), ExperimentTracker(sf)
        self.log = log or (lambda msg: None)
        self.stop_event = stop_event or threading.Event()
        self.phase = "idle"
        self.session_trials = 0
        self._engine: _ConsistencyEvolution | None = None
        self._last_ai_rejections: list[str] = []
        self._imported = False
        self._run_started = datetime.now(timezone.utc).replace(tzinfo=None)
        self._adopt_legacy_rows()
        self._record_universe()
        self.rejudge_finals()

    def _record_universe(self) -> None:
        with self.sf() as s, s.begin():
            if s.get(m.ResearchUniverse, self.universe_id) is None:
                s.add(m.ResearchUniverse(id=self.universe_id, key=self.universe_key))

    def previous_ranking(self) -> dict | None:
        """The most recently used OTHER ranking with searched strategies: they are kept, and the first thing a new
        search does is re-score the best of them on the current data (import_previous)."""
        R = m.ResearchCandidate
        with self.sf() as s:
            row = s.execute(select(R.universe_id, func.count(), func.max(R.created_at), func.max(R.fitness))
                            .where(R.universe_id.is_not(None), R.universe_id != self.universe_id,
                                   R.origin.in_(("evolution", "ai")))
                            .group_by(R.universe_id).order_by(func.max(R.created_at).desc()).limit(1)).first()
            if row is None:
                return None
            old = s.get(m.ResearchUniverse, row[0])
            n_old = len((old.key or {}).get("symbols") or {}) if old else None
        return {"n": int(row[1]), "last": str(row[2])[:16] if row[2] else None, "best_consistency": row[3],
                "n_symbols": n_old, "changes": universe_changes(old.key if old else None, self.universe_key)}

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
            allr = pd.concat({k: v["close"].pct_change() for k, v in self.research.items()}, axis=1)
            allr = allr[(allr.index >= self.start) & (allr.index <= self.search_end)]
            rets = allr.mean(axis=1, skipna=True).fillna(0.0)
            eq = (1 + rets).cumprod() * self.bt.initial_capital
            def sharpe(r):
                sd = r.std(ddof=1)
                return float(r.mean() / sd * np.sqrt(self.bt.bars_per_year)) if sd > 0 else 0.0
            blocks = [rets.iloc[b] for b in np.array_split(np.arange(len(rets)), self.cfg.blocks)]
            half_blocks = []  # scored exactly like a strategy: worst block of the full set and of both halves
            for h in self.halves:
                hr = allr[[c for c in h if c in allr.columns]].mean(axis=1, skipna=True).fillna(0.0)
                half_blocks += [hr.iloc[b] for b in np.array_split(np.arange(len(hr)), self.cfg.blocks)]
            yearly = periodic_returns(eq, "YE")
            self._passive = _clean({
                "rules": f"Mantener las {len(self.research)} acciones a partes iguales, sin hacer nada",
                "consistency": min(sharpe(b) for b in blocks + half_blocks), "sharpe": sharpe(rets),
                "cagr": float((eq.iloc[-1] / eq.iloc[0]) ** (self.bt.bars_per_year / max(len(eq) - 1, 1)) - 1),
                "max_drawdown": float((eq / eq.cummax() - 1).min()), "pct_positive_years": float((yearly > 0).mean()),
                "worst_year": float(yearly.min()),
                "blocks": [{"start": str(b.index[0].date()), "end": str(b.index[-1].date()), "sharpe": sharpe(b),
                            "return": float((1 + b).prod() - 1)} for b in blocks]})
        return self._passive

    def pre_exam(self, sd: StrategyDefinition, search_sharpe: float | None) -> dict:
        """'Examen previo' on the last research years the search never optimised on, judged with the SAME rules as
        the final test: makes money, beats holding the same stocks (Sharpe), keeps half of its search Sharpe."""
        _, mh = run_window(sd, self.research, self.bt, self.holdout_start, self.end)
        rets = pd.concat({k: v["close"].pct_change() for k, v in self.research.items()}, axis=1)
        rets = rets[(rets.index >= self.holdout_start) & (rets.index <= self.end)].mean(axis=1, skipna=True)
        passive = window_stats(rets, self.bt.bars_per_year)
        metrics = {k: mh.get(k) for k in ("total_return", "sharpe", "max_drawdown", "n_trades")}
        verdict = final_verdict(metrics, search_sharpe, passive)
        return _clean({"period": [str(self.holdout_start.date()), str(self.end.date())], "metrics": metrics,
                       "passive": passive, "search_sharpe": search_sharpe, **verdict})

    def baseline_sharpes(self) -> dict:
        """Sharpe of the simple reference strategies on the research window (the same for every validation)."""
        if getattr(self, "_baselines", None) is None:
            self._baselines = {b.name: run_window(b, self.research, self.bt, self.start, self.search_end)[1].get("sharpe")
                               for b in (momentum_baseline(), trend_baseline())}
        return dict(self._baselines)

    def oos_benchmarks(self, symbols: list[str] | None = None) -> dict:
        """Buy & hold references over the OOS period (the strategy's stocks equal weight; benchmark). Only used to
        judge a final test; never shown to the search or the AI."""
        cache = self.__dict__.setdefault("_oos_bench", {})
        frames = {k: self.full[k] for k in (symbols or self.full) if k in self.full} or self.full
        key = tuple(sorted(frames))
        if key not in cache:
            oos = pd.Timestamp(self.cfg.oos_start, tz="UTC")
            end = max(v.index.max() for v in self.full.values())
            rets = pd.concat({k: v["close"].pct_change() for k, v in frames.items()}, axis=1)
            rets = rets[(rets.index >= oos) & (rets.index <= end)].mean(axis=1, skipna=True)
            out = {"passive": {**window_stats(rets, self.bt.bars_per_year), "n_symbols": len(frames)}}
            if self.benchmark is not None:
                b = self.benchmark.pct_change()
                out["benchmark"] = window_stats(b[(b.index >= oos) & (b.index <= end)], self.bt.bars_per_year)
            cache[key] = _clean(out)
        return cache[key]

    def _row_symbols(self, row) -> list[str] | None:
        """Stocks the strategy was validated on (its validation experiment), if recorded."""
        exp = (row.validation or {}).get("experiment_id")
        if not exp:
            return None
        with self.sf() as s:
            e = s.get(m.Experiment, exp)
        return list((e.config or {}).get("symbols") or []) or None if e is not None else None

    def _judge(self, row, oos: dict) -> dict:
        bench = self.oos_benchmarks(self._row_symbols(row))
        research_sharpe = ((row.validation or {}).get("is_metrics") or {}).get("sharpe") or (row.metrics or {}).get("sharpe")
        v = final_verdict(oos, research_sharpe, bench.get("passive"))
        return {**v, "research_sharpe": research_sharpe, "passive": bench.get("passive"),
                "benchmark": bench.get("benchmark"), "criteria_version": FINAL_CRITERIA_VERSION}

    def rejudge_finals(self) -> int:
        """Final tests judged under the old, too lenient rule (only 'made money') are re-judged with the current
        criteria from their STORED out-of-sample metrics (the vault is not opened again). All rankings: an approval
        from an earlier ranking must not stay approved (or in simulation) just because the data changed since."""
        R = m.ResearchCandidate
        with self.sf() as s:
            rows = s.scalars(select(R).where(R.status.in_(("FINAL_PASS", "FINAL_FAIL")))).all()
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

    def _top_rows(self, n: int, origin: str | None = None, statuses: tuple[str, ...] | None = None,
                  since=None) -> list:
        with self.sf() as s:
            q = select(m.ResearchCandidate).where(m.ResearchCandidate.fitness.is_not(None),
                                                  m.ResearchCandidate.universe_id == self.universe_id)
            if since is not None:
                q = q.where(m.ResearchCandidate.created_at >= since)
            if origin:
                q = q.where(m.ResearchCandidate.origin == origin)
            if statuses:
                q = q.where(m.ResearchCandidate.status.in_(statuses))
            return list(s.scalars(q.order_by(m.ResearchCandidate.fitness.desc()).limit(n)))

    def diverse_elites(self) -> list[StrategyDefinition]:
        """Best earlier strategies to seed a cycle, at most `elites_per_idea` built on the same indicators, so one
        good idea cannot fill every seed with variants of itself."""
        out, per = [], {}
        since = self._run_started if self.cfg.fresh_start else None  # fresh: only what THIS run has found
        for r in self._top_rows(self.cfg.elites_from_history * 40, "evolution", since=since):
            sd = definition_from_dict(r.definition)
            if not self.simple_enough(sd) or not self.holding_ok(sd):
                continue
            k = idea(sd)
            if per.get(k, 0) < self.cfg.elites_per_idea:
                per[k] = per.get(k, 0) + 1
                out.append(sd)
            if len(out) >= self.cfg.elites_from_history:
                break
        return out

    def next_cycle(self) -> int:
        with self.sf() as s:
            return int(s.scalar(select(func.max(m.ResearchCandidate.cycle))) or 0) + 1

    # -------------------------------------------------------------- one cycle
    def engine(self) -> _ConsistencyEvolution:
        if self._engine is None:
            self.phase = "preparando datos"
            self._engine = _ConsistencyEvolution(
                self, self.research, (self.start, self.search_end), self.bt,
                EvolutionConfig(population=self.cfg.population, generations=self.cfg.generations, seed=self.cfg.seed,
                                init_hold_choices=self._holds(), hold_choices=self._holds(),
                                complexity_penalty=self.cfg.complexity_penalty, immigrants=self.cfg.immigrants,
                                max_conditions=self.cfg.max_conditions,
                                max_feature_share=self.cfg.max_feature_share))
        return self._engine

    def _holds(self) -> tuple:
        return tuple(h for h in (2, 3, 5, 7, 10, 15, 20) if h <= self.cfg.max_holding_days) or (self.cfg.max_holding_days,)

    def simple_enough(self, sd: StrategyDefinition) -> bool:
        return len(sd.entry_long) + len(sd.entry_short) <= self.cfg.max_conditions

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
            # a version that already had its one-time final test is finished (passed -> Simulación; failed -> rejected)
            seen = set(s.scalars(select(R.version_id).where(R.status.in_(("FINAL_PASS", "FINAL_FAIL")))).all())
        picked = []
        for r in prev:
            vid = r.version_id or r.id
            if vid not in seen:
                seen.add(vid)
                picked.append(r)
            if len(picked) >= n:
                break
        picked = [r for r in picked if self.holding_ok(sd := definition_from_dict(r.definition)) and self.simple_enough(sd)]
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
        if not self.cfg.fresh_start:
            self.phase = "reaprovechando investigación anterior"
            self.import_previous()
        self.phase = "búsqueda evolutiva"
        eng = self.engine()
        eng.cycle = cycle
        eng.rng = np.random.default_rng([self.cfg.seed, cycle])
        elites = self.diverse_elites()
        ideas = sorted({idea(sd) for sd in elites})
        self.log(f"Ciclo {cycle}: evolución ({self.cfg.population}×{self.cfg.generations}) partiendo de {len(elites)} "
                 f"mejores anteriores ({len(ideas)} ideas distintas: {', '.join(ideas)[:120]})")
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
        self._run_started = datetime.now(timezone.utc).replace(tzinfo=None)
        if self.cfg.fresh_start:
            self.log("Modo 'partir de cero': no se usan las mejores estrategias anteriores como punto de partida")
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
        best, ideas = [], {}
        for r in self._top_rows(300):  # the best strategy of each idea, and how crowded each idea is
            sd = definition_from_dict(r.definition)
            k = idea(sd)
            ideas[k] = ideas.get(k, 0) + 1
            if ideas[k] > 1 or len(best) >= 6:
                continue
            mt = r.metrics or {}
            best.append({"rules": describe(sd), "origin": r.origin, "consistency_score": round(r.fitness, 3),
                         "sharpe": mt.get("sharpe"), "block_sharpes": [b["sharpe"] for b in mt.get("blocks", [])],
                         "pct_positive_years": mt.get("pct_positive_years"), "n_trades": mt.get("n_trades")})
        crowded = [k for k, c in sorted(ideas.items(), key=lambda x: -x[1]) if c >= 0.3 * sum(ideas.values())]
        with self.sf() as s:
            reasons = s.scalars(select(m.ResearchCandidate.error).where(m.ResearchCandidate.status == "INVALID",
                                                                         m.ResearchCandidate.universe_id == self.universe_id)
                                .order_by(m.ResearchCandidate.created_at.desc()).limit(200)).all()
        failures: dict[str, int] = {}
        for x in reasons:
            key = (x or "error").split("(")[0].strip()[:60]
            failures[key] = failures.get(key, 0) + 1
        return {"goal": ("Find LONG-ONLY daily swing-trading rules for these US large caps that are CONSISTENT: the "
                         f"score is the WORST annualised Sharpe across {self.cfg.blocks} consecutive sub-periods of the "
                         "research window" + (", computed separately on two random halves of the stocks (the rule must "
                                              "work on both)" if self.halves else "") +
                         f", minus {self.cfg.complexity_penalty} per complexity point. At most "
                         f"{self.cfg.max_conditions} entry conditions, few parameters, at least {self.cfg.min_trades} "
                         f"trades. Every trade must close within {self.cfg.max_holding_days} trading days. Prefer simple, "
                         "economically sensible ideas: complicated rules memorise the past and fail on new data."),
                "universe": sorted(self.research), "research_period": [str(self.start.date()), str(self.search_end.date())],
                "best_so_far": best, "recent_failure_reasons": failures, "trials_so_far": n,
                "indicator_ideas_among_top_results": ideas,
                "diversity_request": (f"Most of the top results are variants built on {', '.join(crowded)}. Do NOT propose "
                                      "more variants of that: propose genuinely different hypotheses with other "
                                      "indicators." if crowded else "Explore genuinely different hypotheses."),
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
            if not self.simple_enough(sd):
                self._last_ai_rejections.append(f"{sd.name}: at most {self.cfg.max_conditions} entry conditions")
                self.log(f"IA: '{sd.name}' descartada (más de {self.cfg.max_conditions} condiciones de compra)")
                continue
            fit, met, new = self.evaluate_and_store(sd, "ai", cycle)
            self.log(f"IA: '{sd.name}' → " + (f"consistencia {fit:.3f}" if fit is not None else
                                              f"no puntuable ({met.get('invalid_reason', 'error')})"))

    # -------------------------------------------------------------- validation (research data only)
    def _validate_finalists(self) -> int:
        pool = self._top_rows(self.cfg.finalist_pool)
        stale = [r for r in pool if r.status == "VALIDATED_PASS" and self._stale_validation(r)]
        todo = (stale + [r for r in pool if r.status == "EVALUATED" and r.origin != "baseline"])[: self.cfg.finalists_per_cycle]
        for r in todo:
            self.check_stop()
            self.validate(r.id)
        return len(todo)

    def _stale_validation(self, row) -> bool:
        """Validated under older rules (no passive gate / no pre-exam): re-check before trusting it."""
        gates = (row.validation or {}).get("gates") or {}
        return "beats_passive" not in gates or (self.holdout_start is not None and "pre_exam" not in gates)

    def validate(self, vid: str) -> dict:
        row = self.get_row(vid)
        sd = definition_from_dict(row.definition)
        self.log(f"Validando {vid[:8]} (varios minutos; el avance se ve en 'Fase'): {describe(sd)[:90]}")
        phase0 = self.phase
        def step(txt):
            def cb(done, total):
                self.check_stop()  # "Detener" also works in the middle of a validation
                self.phase = f"validando {vid[:8]}: {txt} {done + 1}/{total}"
            return cb
        n_trials, var_sr = self.trial_stats()
        # every check below uses the SEARCH window only; the pre-exam period is used once, at the end
        res, mt = run_window(sd, self.research, self.bt, self.start, self.search_end)
        folds = make_folds(self.search_index, self.cfg.wf_train, self.cfg.wf_validate, self.cfg.wf_test,
                           self.cfg.embargo)
        with signal_cache(max_items=2 * len(self.research) + 10):  # one strategy, many windows: signals once
            wf = walk_forward(sd, self.research, {k: [v] for k, v in sd.params.items()}, folds, self.bt,
                              progress=step("ventanas móviles")) if folds else None
        rob = parameter_robustness(sd, self.research, self.bt, self.start, self.search_end,
                                   progress=step("cambios de parámetros")) if sd.params else None
        step("costes dobles y referencias")(0, 1)
        mc = monte_carlo_trades(res.trades, self.bt.initial_capital, self.cfg.mc_sims, self.cfg.seed)
        costs = cost_sensitivity(sd, self.research, self.bt, self.start, self.search_end, multipliers=(1.0, 2.0))
        base = self.baseline_sharpes()
        pre = self.pre_exam(sd, mt.get("sharpe")) if self.holdout_start is not None else None
        self.phase = phase0
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
        if pre is not None:
            gates["pre_exam"] = pre["passed"]
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
                      "pre_exam": pre,
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
        if row.status == "VALIDATED_PASS" and self._stale_validation(row):
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
    def leaderboard(self, limit: int = 20, group: bool = False) -> dict:
        """`group`: one row per indicator idea (its best variant; validated / final-tested rows always shown), with
        the number of similar variants hidden behind it."""
        n, var = self.trial_stats()
        passive = self.passive_reference()
        # "No skill" for a long-only stock strategy is not a zero Sharpe: it is the Sharpe of simply holding the same
        # stocks. Fiabilidad = P(true Sharpe > holding + what the best of n unskilled tries shows by luck).
        sr0 = max(0.0, (passive.get("sharpe") or 0.0) / np.sqrt(self.bt.bars_per_year)) + expected_max_sharpe(max(n, 1), var)
        with self.sf() as s:
            finals = s.scalar(select(func.count()).select_from(m.OOSAccessLog)) or 0
        rows, seen, hidden, by_idea = [], set(), 0, {}
        for r in self._top_rows(limit * (40 if group else 4)):
            mt = r.metrics or {}
            # logically equivalent rule sets (e.g. a redundant extra condition) trade identically: show the
            # best-ranked one only (the complexity penalty already ranks the simpler one first)
            sig = (mt.get("n_trades"), round(mt.get("sharpe") or 0, 9), round(mt.get("max_drawdown") or 0, 9))
            if sig in seen:
                hidden += 1
                continue
            seen.add(sig)
            sd = definition_from_dict(r.definition)
            k = idea(sd)
            if group and k in by_idea and r.status not in ("VALIDATED_PASS", "FINAL_PASS", "FINAL_FAIL"):
                by_idea[k]["variants"] += 1  # a variant of an idea already shown
                continue
            if len(rows) >= limit:
                continue
            dsr = psr_from_stats(mt["sr"], mt["skew"], mt["kurt"], mt["T"], sr0) if mt.get("T") else None
            rows.append({"id": r.id, "origin": r.origin, "cycle": r.cycle, "rules": describe(sd), "name": sd.name,
                         "idea": k, "variants": 0,
                         "consistency": r.fitness, "sharpe": mt.get("sharpe"), "cagr": mt.get("cagr"),
                         "max_drawdown": mt.get("max_drawdown"), "n_trades": mt.get("n_trades"),
                         "pct_positive_years": mt.get("pct_positive_years"), "worst_year": mt.get("worst_year"),
                         "avg_days": mt.get("avg_trade_bars"),
                         "blocks": mt.get("blocks"), "halves": mt.get("halves"), "dsr": dsr, "status": r.status,
                         "strategy_id": r.strategy_id,
                         "validation": r.validation, "final": r.final})
            by_idea.setdefault(k, rows[-1])
        R = m.ResearchCandidate
        with self.sf() as s:
            by_status = dict(s.execute(select(R.status, func.count()).where(R.universe_id == self.universe_id)
                                       .group_by(R.status)).all())
        return _clean({"n_trials": n, "n_trials_universe": int(sum(by_status.values())), "final_tests_used": finals,
                       "universe": {"id": self.universe_id, "n_symbols": len(self.research),
                                    "with_earnings": sum("earn_days_to" in v.columns for v in self.research.values())},
                       "earnings_rule": {"blackout_days": self.bt.earnings_blackout_days,
                                         "exit_before": self.bt.exit_before_earnings},
                       "passive": passive, "previous": self.previous_ranking(),
                       "by_status": by_status, "equivalents_hidden": hidden, "grouped": group,
                       "research_period": [str(self.start.date()), str(self.end.date())], "oos_start": self.cfg.oos_start,
                       "search_period": [str(self.start.date()), str(self.search_end.date())],
                       "pre_exam_period": [str(self.holdout_start.date()), str(self.end.date())] if self.holdout_start is not None else None,
                       "rules": {"halves": bool(self.halves), "max_conditions": self.cfg.max_conditions,
                                 "max_holding_days": self.cfg.max_holding_days,
                                 "complexity_penalty": self.cfg.complexity_penalty},
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
