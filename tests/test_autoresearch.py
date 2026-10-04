"""Automatic research loop. SYNTHETIC random-walk data only: nothing here is market evidence."""
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from conftest import synthetic_daily
from qsts.ai.providers import MockAIProvider
from qsts.ai.service import AIResearchService
from qsts.backtest.benchmarks import momentum_baseline
from qsts.backtest.engine import BacktestConfig
from qsts.data.bars import Timeframe, to_canonical
from qsts.db import models as m
from qsts.research import autoresearch as ar
from qsts.research.autoresearch import AutoResearchConfig, AutoResearcher, describe
from qsts.research.evolution import EvolutionConfig, EvolutionEngine
from qsts.research.experiments import ExperimentTracker
from qsts.research.validation import OOSAccessDenied, OOSVault
from qsts.strategy.definition import (Condition, F, StopRule, StrategyDefinition, TakeProfitRule, V,
                                     definition_from_dict)

OOS = "2022-01-01"
CFG = AutoResearchConfig(oos_start=OOS, population=6, generations=1, finalists_per_cycle=1, mc_sims=100,
                         use_ai=True, ai_proposals=2)

AI_JSON = json.dumps({"strategies": [
    {"name": "ai_rsi_dip", "family": "mean_reversion", "hypothesis": "short dips revert", "direction": "long",
     "entry_long": [{"left": {"feature": "rsi", "params": {"n": 14}}, "op": "<", "right": {"value": 45}}],
     "exit_long": [{"left": {"feature": "rsi", "params": {"n": 14}}, "op": ">", "right": {"value": 55}}],
     "stop": {"kind": "atr", "atr_n": 14, "mult": 2.0}, "take_profit": {"kind": "none"}, "max_holding_bars": 10,
     "params": {}},
    {"name": "ai_short", "family": "x", "hypothesis": "h", "direction": "short",
     "entry_short": [{"left": {"feature": "rsi", "params": {"n": 14}}, "op": ">", "right": {"value": 70}}],
     "stop": {"kind": "atr", "atr_n": 14, "mult": 2.0}, "take_profit": {"kind": "none"}, "params": {}},
    {"name": "bad_format", "family": "x", "hypothesis": "h", "direction": "long",
     "entry_long": [{"left": "close", "op": ">", "right": {"value": 1}}], "params": {}},
]})


@pytest.fixture(scope="module")
def data():
    return {s: to_canonical(synthetic_daily("2014-01-01", "2023-06-30", seed=40 + i), Timeframe.D1)
            for i, s in enumerate(["AAA", "BBB", "CCC"])}


def researcher(sf, data, cfg=CFG, prompts=None):
    def respond(system, prompt):
        if prompts is not None:
            prompts.append(prompt)
        return AI_JSON
    ai = AIResearchService(MockAIProvider(respond), sf, max_calls_per_day=10)
    return AutoResearcher(sf, data, cfg, BacktestConfig(), ai=ai)


def count(sf, model):
    with sf() as s:
        return s.scalar(select(func.count()).select_from(model))


def test_cycle_uses_research_data_only_and_counts_every_trial(sf, data):
    prompts = []
    r = researcher(sf, data, prompts=prompts)
    assert max(df.index.max() for df in r.research.values()) < pd.Timestamp(OOS, tz="UTC")
    out = r.run_cycle(1)
    n = count(sf, m.ResearchCandidate)
    assert out["new_trials"] == n == r.session_trials > CFG.population
    assert count(sf, m.OOSAccessLog) == 0  # the search never opens the vault
    lb = r.leaderboard(50)
    fits = [x["consistency"] for x in lb["rows"]]
    assert fits == sorted(fits, reverse=True) and lb["n_trials"] == n
    with sf() as s:
        origins = set(s.scalars(select(m.ResearchCandidate.origin).distinct()))
    assert {"evolution", "baseline", "ai"} <= origins
    with sf() as s:  # only the valid long-only AI proposal was backtested
        ai_rows = s.scalars(select(m.ResearchCandidate).where(m.ResearchCandidate.origin == "ai")).all()
    assert [row.definition["name"] for row in ai_rows] == ["ai_rsi_dip"]
    assert out["validated"] == 1 and any(x["status"].startswith("VALIDATED") for x in lb["rows"])
    # re-evaluating a known version is not a new trial
    sd = momentum_baseline()
    assert r.evaluate_and_store(sd, "baseline", 2)[2] is False and count(sf, m.ResearchCandidate) == n
    # the AI saw research-period results, never anything from the vault
    assert prompts and "oos" not in prompts[0].lower().replace("boost", "")


def test_consistency_is_worst_block(sf, data):
    r = researcher(sf, data)
    fit, met = r.evaluator.evaluate(momentum_baseline())
    assert len(met["blocks"]) == CFG.blocks
    if fit is not None:
        cx = momentum_baseline().complexity()["score"]
        assert fit == pytest.approx(min(b["sharpe"] for b in met["blocks"]) - CFG.complexity_penalty * cx)
    rare = StrategyDefinition(name="rare", family="t", hypothesis="h",
                              entry_long=(Condition(F("rsi", n=14), "<", V(1.0)),), stop=StopRule("atr", 14, 2.0),
                              take_profit=TakeProfitRule("none"))
    fit, met = r.evaluator.evaluate(rare)
    assert fit is None and "pocas operaciones" in met["invalid_reason"]


def test_more_trials_lower_the_deflated_sharpe(sf, data):
    r = researcher(sf, data, cfg=AutoResearchConfig(**{**CFG.__dict__, "use_ai": False}))
    r.seed_baselines()
    before = {x["id"]: x["dsr"] for x in r.leaderboard()["rows"]}
    r.engine().run()
    after = {x["id"]: x["dsr"] for x in r.leaderboard()["rows"]}
    common = [k for k in before if before[k] is not None and after.get(k) is not None]
    assert common and all(after[k] <= before[k] + 1e-12 for k in common)


def test_final_test_only_after_validation_and_only_once(sf, data, monkeypatch):
    r = researcher(sf, data, cfg=AutoResearchConfig(**{**CFG.__dict__, "use_ai": False}))
    r.seed_baselines()
    r.engine().run()
    vid = r.leaderboard(1)["rows"][0]["id"]
    with pytest.raises(ValueError):
        r.final_test(vid)  # not validated yet
    # force the research gates open so the OOS path is exercised deterministically
    monkeypatch.setattr(ar, "strategy_score", lambda **kw: {"gates": {}, "score": 0.5})
    monkeypatch.setattr(ar, "cost_sensitivity", lambda *a, **k: [{"cost_multiplier": 2.0, "sharpe": 1.0}])
    monkeypatch.setattr(ar, "parameter_robustness",
                        lambda *a, **k: SimpleNamespace(passed=True, stability=1.0, peak_sharpness=0.0))
    val = r.validate(vid)
    assert val["passed"] is True
    final = r.final_test(vid)
    assert final["decision"] in ("FINAL_PASS", "FINAL_FAIL") and count(sf, m.OOSAccessLog) == 1
    with pytest.raises(ValueError):
        r.final_test(vid)  # status changed: no second look
    with pytest.raises(OOSAccessDenied):  # the vault itself refuses a second look at this version
        r.vault.evaluate(definition_from_dict(_definition(sf, vid)), data, BacktestConfig(), purpose="again")
    # the AI context never carries final-test results
    assert "final" not in json.dumps(r.ai_context()).lower()


def _definition(sf, vid):
    with sf() as s:
        return s.get(m.ResearchCandidate, vid).definition


def test_evolution_seeds_initial_population(data):
    tr = (pd.Timestamp("2015-01-02", tz="UTC"), pd.Timestamp("2018-12-31", tz="UTC"))
    va = (pd.Timestamp("2019-01-15", tz="UTC"), pd.Timestamp("2021-12-31", tz="UTC"))
    eng = EvolutionEngine(data, tr, va, cfg=EvolutionConfig(population=4, generations=0, seed=1))
    seed = eng.random_individual()
    eng2 = EvolutionEngine(data, tr, va, cfg=EvolutionConfig(population=4, generations=0, seed=2))
    eng2.run(initial=[seed])
    assert seed.version_id in eng2._cache


def test_describe_and_reproduce_research_view(sf, data):
    sd = StrategyDefinition(name="d", family="f", hypothesis="h",
                            entry_long=(Condition(F("roc"), ">", V("$p0")),),
                            exit_long=(Condition(F("close"), "<", F("sma", n=50)),),
                            stop=StopRule("atr", 14, 2.0), take_profit=TakeProfitRule("r_multiple", 2.0),
                            max_holding_bars=10, params={"p0": 1.5})
    txt = describe(sd)
    assert txt.startswith("Compra si roc(") and "sale si close < sma(50)" in txt and "máx. 10 días" in txt
    vault = OOSVault(sf, OOS)
    research = vault.research_view(data)
    end = max(v.index.max() for v in research.values())
    t = ExperimentTracker(sf)
    rec = t.run_backtest(momentum_baseline(), research, BacktestConfig(), start=None, end=end)
    assert t.reproduce(rec.id, data)["reproduced"] is True  # full data in, research view rebuilt
