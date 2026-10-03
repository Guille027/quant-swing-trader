"""Look-ahead detection by truncation.

If a strategy (or any feature it uses) peeks into the future, then its decisions at bar t will
change when bars after t are removed. We evaluate on several truncated histories and require the
decisions to be identical on the common prefix.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from qsts.strategy.definition import CompiledStrategy, StrategyDefinition


class LookAheadError(AssertionError):
    pass


def check_strategy_causality(sd: StrategyDefinition, df: pd.DataFrame, n_cuts: int = 5, seed: int = 0,
                             min_history: int = 260) -> None:
    cs = CompiledStrategy(sd)

    def evaluate(d: pd.DataFrame) -> pd.DataFrame:
        # decisions AND the raw feature values: a peeking feature may not flip a decision on
        # every bar, but its value (or NaN-ness) at the cut always changes.
        return pd.concat([cs.evaluate(d), cs.fs.compute(d).add_prefix("feat:")], axis=1)

    full = evaluate(df)
    rng = np.random.default_rng(seed)
    if len(df) <= min_history + 1:
        raise ValueError("not enough data for causality check")
    cuts = sorted(rng.choice(np.arange(min_history, len(df)), size=min(n_cuts, len(df) - min_history), replace=False))
    for cut in cuts:
        part = evaluate(df.iloc[:cut])
        a, b = part, full.iloc[:cut]
        for col in a.columns:
            x, y = a[col].to_numpy(dtype=float), b[col].to_numpy(dtype=float)
            same = (np.isclose(x, y, rtol=1e-9, atol=1e-12) | (np.isnan(x) & np.isnan(y)))
            if not same.all():
                first = a.index[np.argmin(same)]
                raise LookAheadError(f"{sd.name}: column {col} at {first} changes when data after bar {cut} is removed")
