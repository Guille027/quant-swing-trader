import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.backtest.engine import BacktestConfig
from qsts.data.bars import Timeframe, to_canonical
from qsts.research.evolution import EvolutionConfig, EvolutionEngine


@pytest.fixture(scope="module")
def data():
    return {s: to_canonical(synthetic_daily("2016-01-01", "2021-12-31", seed=10 + i), Timeframe.D1)
            for i, s in enumerate(["AAA", "BBB"])}


TRAIN = (pd.Timestamp("2016-06-01", tz="UTC"), pd.Timestamp("2019-06-30", tz="UTC"))
VAL = (pd.Timestamp("2019-07-15", tz="UTC"), pd.Timestamp("2021-12-31", tz="UTC"))


def engine(data, seed=0, **kw):
    return EvolutionEngine(data, TRAIN, VAL, BacktestConfig(),
                           EvolutionConfig(population=8, generations=2, seed=seed, **kw))


def test_rejects_overlapping_windows(data):
    with pytest.raises(ValueError):
        EvolutionEngine(data, VAL, TRAIN)


def test_genome_ops_produce_valid_strategies(data):
    e = engine(data)
    for _ in range(30):
        a, b = e.random_individual(), e.random_individual()
        for sd in (a, e.mutate(a), e.crossover(a, b)):
            sd.validate()
            assert 1 <= len(sd.entry_long) <= 3
            assert sd.complexity()["n_params"] == len(sd.entry_long)


def test_deterministic_and_counts_trials(data):
    r1 = engine(data, seed=5).run()
    r2 = engine(data, seed=5).run()
    assert [i.sd.version_id for i in r1["best"]] == [i.sd.version_id for i in r2["best"]]
    assert r1["n_trials"] >= 8 and len(r1["history"]) == 2
    for ind in r1["best"]:
        assert ind.fitness <= min(ind.train, ind.val)  # penalties only ever reduce fitness
