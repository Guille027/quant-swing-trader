"""Shared fixtures. All price series here are SYNTHETIC test fixtures (random walks), never market data."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qsts.data.bars import Timeframe, nyse_schedule, nyse_sessions
from qsts.db.session import init_db, make_engine, session_factory


def synthetic_daily(start="2020-01-01", end="2022-12-31", seed=0, drift=0.0003, vol=0.015, s0=100.0):
    rng = np.random.default_rng(seed)
    idx = nyse_sessions(start, end)
    r = rng.normal(drift, vol, len(idx))
    close = s0 * np.exp(np.cumsum(r))
    open_ = np.r_[s0, close[:-1]] * np.exp(rng.normal(0, vol / 3, len(idx)))
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, len(idx))))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, len(idx))))
    v = rng.integers(1_000_000, 5_000_000, len(idx)).astype(float)
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": v}, index=idx)


def synthetic_hourly(start="2024-03-01", end="2024-03-29", seed=0):
    rng = np.random.default_rng(seed)
    sched = nyse_schedule(start, end)
    stamps = []
    for _, row in sched.iterrows():
        t = row["market_open"]
        while t < row["market_close"]:
            stamps.append(t)
            t += pd.Timedelta(hours=1)
    idx = pd.DatetimeIndex(stamps).tz_convert("UTC")
    close = 50 * np.exp(np.cumsum(rng.normal(0, 0.004, len(idx))))
    open_ = np.r_[50, close[:-1]]
    hi = np.maximum(open_, close) * 1.002
    lo = np.minimum(open_, close) * 0.998
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close,
                         "volume": rng.integers(1e4, 1e5, len(idx)).astype(float)}, index=idx)


@pytest.fixture
def daily():
    return synthetic_daily()


@pytest.fixture
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(eng)
    return session_factory(eng)
