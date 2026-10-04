"""Declarative, versioned, reproducible strategy definitions.

A strategy is pure data (JSON-serialisable). Its version id is a content hash, so an identical
definition always maps to the same id and any change creates a new version. Rules reference
features by spec and tunable values by "$param" placeholders, which lets the optimiser, the
robustness tester and the evolutionary engine vary parameters without touching rule structure.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from qsts.core.hashing import hash_obj
from qsts.features.registry import REGISTRY, FeatureSet, FeatureSpec

Op = Literal["<", ">", "<=", ">=", "cross_above", "cross_below"]
PRICE_FIELDS = {"open", "high", "low", "close", "volume"}


@dataclass(frozen=True)
class Operand:
    """Either a feature reference, a price field, a literal number or a "$param"."""
    feature: str | None = None
    params: dict = field(default_factory=dict)
    value: float | str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, {})}


def F(name: str, **params) -> Operand:
    return Operand(feature=name, params=params)


def V(v: float | str) -> Operand:
    return Operand(value=v)


@dataclass(frozen=True)
class Condition:
    left: Operand
    op: Op
    right: Operand


@dataclass(frozen=True)
class StopRule:
    kind: Literal["atr", "percent", "structure"] = "atr"
    atr_n: int | str = 14
    mult: float | str = 2.0  # atr multiple, or percent (0.05 = 5%) for kind="percent"
    structure_k: int | str = 3
    trailing: bool = False


@dataclass(frozen=True)
class TakeProfitRule:
    kind: Literal["none", "r_multiple", "atr"] = "r_multiple"
    value: float | str = 2.0


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    family: str
    hypothesis: str
    timeframe: str = "1d"
    direction: Literal["long", "short", "both"] = "long"
    entry_long: tuple[Condition, ...] = ()
    entry_short: tuple[Condition, ...] = ()
    exit_long: tuple[Condition, ...] = ()  # ANY condition true -> exit
    exit_short: tuple[Condition, ...] = ()
    stop: StopRule = StopRule()
    take_profit: TakeProfitRule = TakeProfitRule()
    max_holding_bars: int | str | None = None
    allowed_regimes: tuple[str, ...] | None = None
    rank_by: Operand | None = None  # when slots are limited; higher = preferred
    params: dict[str, float] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ identity
    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("metadata")
        return d

    @property
    def version_id(self) -> str:
        return hash_obj(self.to_dict())

    def with_params(self, **updates) -> "StrategyDefinition":
        unknown = set(updates) - set(self.params)
        if unknown:
            raise KeyError(f"unknown params {unknown}")
        return _replace(self, params={**self.params, **updates})

    # ------------------------------------------------------------------ complexity
    def complexity(self) -> dict:
        conds = [*self.entry_long, *self.entry_short, *self.exit_long, *self.exit_short]
        feats = {o.feature for c in conds for o in (c.left, c.right) if o.feature and o.feature not in PRICE_FIELDS}
        return {"n_params": len(self.params), "n_rules": len(conds), "n_features": len(feats),
                "score": len(self.params) + len(conds) + len(feats)}

    # ------------------------------------------------------------------ resolution
    def resolve(self, x: Any) -> Any:
        if isinstance(x, str) and x.startswith("$"):
            key = x[1:]
            if key not in self.params:
                raise KeyError(f"undefined param {x}")
            return self.params[key]
        return x

    def _spec(self, o: Operand) -> FeatureSpec:
        params = {k: self.resolve(v) for k, v in o.params.items()}
        # integral floats (e.g. from optimisers) become ints so window lengths stay valid
        params = {k: int(v) if isinstance(v, float) and v.is_integer() else v for k, v in params.items()}
        return FeatureSpec(o.feature, params)

    def integer_params(self) -> set[str]:
        """Params used where only an integer makes sense (window lengths, bar counts). Optimisers and the
        robustness scan must vary these in integer steps: params are stored as floats."""
        out = set()

        def ref(v):
            return v[1:] if isinstance(v, str) and v.startswith("$") else None
        for c in [*self.entry_long, *self.entry_short, *self.exit_long, *self.exit_short]:
            for o in (c.left, c.right):
                defaults = REGISTRY[o.feature].defaults if o.feature in REGISTRY else {}
                for k, v in o.params.items():
                    if ref(v) and isinstance(defaults.get(k), int) and not isinstance(defaults.get(k), bool):
                        out.add(ref(v))
        for v in (self.stop.atr_n, self.stop.structure_k, self.max_holding_bars):
            if ref(v):
                out.add(ref(v))
        return out & set(self.params)

    def feature_set(self) -> FeatureSet:
        specs = {}
        for c in [*self.entry_long, *self.entry_short, *self.exit_long, *self.exit_short]:
            for o in (c.left, c.right):
                if o.feature and o.feature not in PRICE_FIELDS:
                    s = self._spec(o)
                    specs[s.key] = s
        if self.rank_by and self.rank_by.feature and self.rank_by.feature not in PRICE_FIELDS:
            s = self._spec(self.rank_by)
            specs[s.key] = s
        return FeatureSet(list(specs.values()))

    def validate(self) -> None:
        if self.direction in ("long", "both") and not self.entry_long:
            raise ValueError("long strategy without entry_long rules")
        if self.direction in ("short", "both") and not self.entry_short:
            raise ValueError("short strategy without entry_short rules")
        for c in [*self.entry_long, *self.entry_short, *self.exit_long, *self.exit_short]:
            for o in (c.left, c.right):
                if o.feature and o.feature not in PRICE_FIELDS and o.feature not in REGISTRY:
                    raise KeyError(f"unknown feature {o.feature}")
                for v in o.params.values():
                    self.resolve(v)
                if o.value is not None:
                    self.resolve(o.value)
        for v in (self.stop.atr_n, self.stop.mult, self.take_profit.value, self.max_holding_bars):
            self.resolve(v)


def _replace(sd: StrategyDefinition, **changes) -> StrategyDefinition:
    d = {f: getattr(sd, f) for f in sd.__dataclass_fields__}
    d.update(changes)
    return StrategyDefinition(**d)


# ---------------------------------------------------------------------- serialisation
def definition_from_dict(d: dict) -> StrategyDefinition:
    d = copy.deepcopy(d)

    def op(x):
        return None if x is None else Operand(**x)

    def conds(xs):
        return tuple(Condition(op(c["left"]), c["op"], op(c["right"])) for c in xs)

    for k in ("entry_long", "entry_short", "exit_long", "exit_short"):
        d[k] = conds(d.get(k, ()))
    d["stop"] = StopRule(**d.get("stop", {}))
    d["take_profit"] = TakeProfitRule(**d.get("take_profit", {}))
    d["rank_by"] = op(d.get("rank_by"))
    if d.get("allowed_regimes") is not None:
        d["allowed_regimes"] = tuple(d["allowed_regimes"])
    return StrategyDefinition(**d)


# ---------------------------------------------------------------------- evaluation
class CompiledStrategy:
    """Evaluates a definition on one symbol's canonical bars -> per-bar decisions at bar CLOSE.

    Output columns:
      long_entry, short_entry, long_exit, short_exit (bool) -- decided at the close of bar t
      stop_dist (price distance from entry to stop, computed with data <= t)
      tp_r (take-profit in R multiples, NaN = none) / tp_dist
      rank (float)
    """

    def __init__(self, sd: StrategyDefinition):
        sd.validate()
        self.sd = sd
        self.fs = sd.feature_set()

    def _series(self, o: Operand, df: pd.DataFrame, feats: pd.DataFrame) -> pd.Series | float:
        if o.feature:
            if o.feature in PRICE_FIELDS:
                return df[o.feature]
            return feats[self.sd._spec(o).key]
        return float(self.sd.resolve(o.value))

    def _cond(self, c: Condition, df, feats) -> pd.Series:
        a, b = self._series(c.left, df, feats), self._series(c.right, df, feats)
        if not isinstance(a, pd.Series):
            a = pd.Series(a, index=df.index)
        if c.op == "<":
            r = a < b
        elif c.op == ">":
            r = a > b
        elif c.op == "<=":
            r = a <= b
        elif c.op == ">=":
            r = a >= b
        else:
            bs = b if isinstance(b, pd.Series) else pd.Series(b, index=df.index)
            if c.op == "cross_above":
                r = (a > bs) & (a.shift(1) <= bs.shift(1))
            else:
                r = (a < bs) & (a.shift(1) >= bs.shift(1))
        nan = a.isna() | (b.isna() if isinstance(b, pd.Series) else False)
        return r & ~nan

    def _all(self, conds, df, feats) -> pd.Series:
        out = pd.Series(True, index=df.index)
        for c in conds:
            out &= self._cond(c, df, feats)
        return out if conds else pd.Series(False, index=df.index)

    def _any(self, conds, df, feats) -> pd.Series:
        out = pd.Series(False, index=df.index)
        for c in conds:
            out |= self._cond(c, df, feats)
        return out

    @staticmethod
    def _atr(df: pd.DataFrame, n: int) -> pd.Series:
        # via the feature registry so the research loop's computation cache applies (same values as ind.atr)
        return FeatureSet([FeatureSpec("atr", {"n": n})]).compute(df).iloc[:, 0]

    def stop_distance(self, df: pd.DataFrame) -> pd.Series:
        from qsts.indicators.structure import swing_points
        s, r = self.sd.stop, self.sd.resolve
        if s.kind == "atr":
            return self._atr(df, int(r(s.atr_n))) * float(r(s.mult))
        if s.kind == "percent":
            return df["close"] * float(r(s.mult))
        sp = swing_points(df, int(r(s.structure_k)))
        # long: distance to last confirmed swing low (shorts use the same magnitude to swing high)
        d_long = (df["close"] - sp["support"]).where(lambda x: x > 0)
        return d_long

    def evaluate(self, df: pd.DataFrame, regime: pd.Series | None = None) -> pd.DataFrame:
        feats = self.fs.compute(df)
        sd = self.sd
        out = pd.DataFrame(index=df.index)
        le = self._all(sd.entry_long, df, feats) if sd.direction in ("long", "both") else pd.Series(False, index=df.index)
        se = self._all(sd.entry_short, df, feats) if sd.direction in ("short", "both") else pd.Series(False, index=df.index)
        if sd.allowed_regimes is not None:
            if regime is None:
                raise ValueError("strategy restricts regimes but no regime series supplied")
            ok = regime.reindex(df.index).isin(sd.allowed_regimes)
            le, se = le & ok, se & ok
        out["long_entry"] = le & ~se
        out["short_entry"] = se & ~le
        out["long_exit"] = self._any(sd.exit_long, df, feats)
        out["short_exit"] = self._any(sd.exit_short, df, feats)
        out["stop_dist"] = self.stop_distance(df)
        tp = sd.take_profit
        if tp.kind == "r_multiple":
            out["tp_dist"] = out["stop_dist"] * float(sd.resolve(tp.value))
        elif tp.kind == "atr":
            out["tp_dist"] = self._atr(df, int(sd.resolve(sd.stop.atr_n))) * float(sd.resolve(tp.value))
        else:
            out["tp_dist"] = np.nan
        if sd.rank_by is not None:
            out["rank"] = self._series(sd.rank_by, df, feats)
        else:
            out["rank"] = 0.0
        # no entry without a valid, positive stop distance
        bad = ~(out["stop_dist"] > 0)
        out.loc[bad, ["long_entry", "short_entry"]] = False
        return out
