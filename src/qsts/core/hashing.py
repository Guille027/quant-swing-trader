"""Deterministic content hashing used for versioning datasets, features, strategies."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def hash_obj(obj: Any, length: int = 16) -> str:
    return hashlib.sha256(stable_json(obj).encode()).hexdigest()[:length]


def hash_frame(df: pd.DataFrame, length: int = 16) -> str:
    h = pd.util.hash_pandas_object(df, index=True).values
    return hashlib.sha256(h.tobytes() + stable_json(list(map(str, df.columns))).encode()).hexdigest()[:length]
