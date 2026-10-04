"""Index member lists for building research universes.

S&P 500 sources, in order:
  1. datasets/s-and-p-500-companies (GitHub CSV, community-maintained, derived from Wikipedia's
     "List of S&P 500 companies"; columns include GICS Sector and "Date added");
  2. Wikipedia's page directly (needs lxml).
These are CURRENT members only. Using them for past years adds survivorship bias (companies that were removed,
acquired or went bankrupt are missing). "Date added" lets research ignore a stock's history before it joined.
"""
from __future__ import annotations

import io
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SP500_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_UA = {"User-Agent": "Mozilla/5.0 (QSTS research app; personal use)"}


@dataclass
class UniverseList:
    name: str
    source: str
    fetched_at: str
    members: pd.DataFrame  # symbol (Yahoo format), name, sector, date_added (Timestamp or NaT)

    def sectors(self) -> dict[str, int]:
        return self.members["sector"].fillna("?").value_counts().to_dict()


def to_yahoo(symbol: str) -> str:
    """Index lists write share classes with a dot (BRK.B); Yahoo uses a dash (BRK-B)."""
    return symbol.strip().upper().replace(".", "-")


def _normalise(df: pd.DataFrame, cols: dict[str, str]) -> pd.DataFrame:
    out = pd.DataFrame({k: df[v] if v in df else None for k, v in cols.items()})
    out["symbol"] = out["symbol"].astype(str).map(to_yahoo)
    out["date_added"] = pd.to_datetime(out["date_added"], errors="coerce")
    out = out[out["symbol"].str.len() > 0].drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)
    if len(out) < 400:  # the S&P 500 has ~500 members; anything far below means the page format changed
        raise ValueError(f"S&P 500 list looks wrong ({len(out)} rows)")
    return out


def parse_sp500_csv(text: str) -> pd.DataFrame:
    return _normalise(pd.read_csv(io.StringIO(text)),
                      {"symbol": "Symbol", "name": "Security", "sector": "GICS Sector", "date_added": "Date added"})


def parse_sp500_wikipedia(html: str) -> pd.DataFrame:
    for t in pd.read_html(io.StringIO(html)):
        if "Symbol" in t.columns and "GICS Sector" in t.columns:
            return _normalise(t, {"symbol": "Symbol", "name": "Security", "sector": "GICS Sector",
                                  "date_added": "Date added"})
    raise ValueError("constituents table not found")


def _get(url: str, timeout: float) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_sp500(timeout: float = 30.0) -> UniverseList:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    errors = []
    for url, parse in ((SP500_CSV, parse_sp500_csv), (SP500_WIKI, parse_sp500_wikipedia)):
        try:
            return UniverseList("SP500", url, now, parse(_get(url, timeout)))
        except Exception as e:  # noqa: BLE001 - try the next source, report all failures
            errors.append(f"{url}: {e!r}")
    raise RuntimeError("could not download the S&P 500 list: " + " | ".join(errors))
