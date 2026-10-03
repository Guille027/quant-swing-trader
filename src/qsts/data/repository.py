"""Persistence for market data, universes and point-in-time datasets."""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from qsts.core.hashing import hash_obj
from qsts.data.bars import Timeframe, compute_available_at
from qsts.data.quality import ValidatedBars
from qsts.db import models as m


def _naive_utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_convert("UTC").tz_localize(None) if t.tz is not None else t


class MarketDataRepository:
    def __init__(self, sf: sessionmaker[Session]):
        self.sf = sf

    # --- assets ------------------------------------------------------------
    def upsert_asset(self, symbol: str, **fields) -> int:
        with self.sf() as s, s.begin():
            a = s.scalar(select(m.Asset).where(m.Asset.symbol == symbol,
                                               m.Asset.exchange == fields.get("exchange")))
            if a is None:
                a = m.Asset(symbol=symbol, **fields)
                s.add(a)
                s.flush()
            else:
                for k, v in fields.items():
                    setattr(a, k, v)
            return a.id

    def asset_id(self, symbol: str) -> int:
        with self.sf() as s:
            ids = s.scalars(select(m.Asset.id).where(m.Asset.symbol == symbol)).all()
        if len(ids) != 1:
            raise KeyError(f"{symbol}: {len(ids)} matching assets")
        return ids[0]

    # --- prices ------------------------------------------------------------
    def store_bars(self, bars: ValidatedBars, source: str) -> int:
        """Only validated bars can be stored. Replaces the overlapping range atomically."""
        if not isinstance(bars, ValidatedBars):
            raise TypeError("store_bars requires ValidatedBars")
        aid = self.asset_id(bars.symbol)
        df = bars.df
        start, end = _naive_utc(df.index.min()), _naive_utc(df.index.max())
        rows = [
            dict(asset_id=aid, timeframe=bars.timeframe.value, ts=_naive_utc(ts),
                 open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume,
                 adj_factor=float(getattr(r, "adj_factor", 1.0)), source=source)
            for ts, r in zip(df.index, df.itertuples(index=False))
        ]
        with self.sf() as s, s.begin():
            s.execute(delete(m.Price).where(m.Price.asset_id == aid, m.Price.timeframe == bars.timeframe.value,
                                            m.Price.ts >= start, m.Price.ts <= end))
            s.execute(m.Price.__table__.insert(), rows)
        return len(rows)

    def load_bars(self, symbol: str, timeframe: Timeframe, start=None, end=None) -> pd.DataFrame:
        """Load canonical raw bars. Re-validate before use in research (ValidatedBars contract)."""
        aid = self.asset_id(symbol)
        q = select(m.Price.ts, m.Price.open, m.Price.high, m.Price.low, m.Price.close, m.Price.volume,
                   m.Price.adj_factor).where(m.Price.asset_id == aid, m.Price.timeframe == timeframe.value)
        if start is not None:
            q = q.where(m.Price.ts >= _naive_utc(start))
        if end is not None:
            q = q.where(m.Price.ts <= _naive_utc(end))
        with self.sf() as s:
            df = pd.DataFrame(s.execute(q.order_by(m.Price.ts)).all(),
                              columns=["ts", "open", "high", "low", "close", "volume", "adj_factor"])
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts")), name="ts").tz_localize("UTC")
        df["available_at"] = compute_available_at(df.index, timeframe)
        return df

    def store_corporate_actions(self, symbol: str, actions: pd.DataFrame, source: str) -> None:
        aid = self.asset_id(symbol)
        with self.sf() as s, s.begin():
            for _, a in actions.iterrows():
                ex = _naive_utc(a["ex_date"]).date()
                s.execute(delete(m.CorporateAction).where(m.CorporateAction.asset_id == aid,
                          m.CorporateAction.ex_date == ex, m.CorporateAction.kind == a["kind"]))
                s.add(m.CorporateAction(asset_id=aid, ex_date=ex, kind=a["kind"], value=float(a["value"]), source=source))

    # --- universe (point-in-time) -------------------------------------------
    def add_membership(self, universe: str, symbol: str, start: date, end: date | None, source: str | None = None):
        aid = self.asset_id(symbol)
        with self.sf() as s, s.begin():
            s.add(m.UniverseMembership(universe=universe, asset_id=aid, start_date=start, end_date=end, source=source))

    def universe_asof(self, universe: str, on: date) -> list[str]:
        """Members on date `on` -- historical, not today's list (survivorship-bias protection)."""
        with self.sf() as s:
            q = (select(m.Asset.symbol).join(m.UniverseMembership, m.UniverseMembership.asset_id == m.Asset.id)
                 .where(m.UniverseMembership.universe == universe, m.UniverseMembership.start_date <= on,
                        or_(m.UniverseMembership.end_date.is_(None), m.UniverseMembership.end_date > on))
                 .distinct().order_by(m.Asset.symbol))
            return list(s.scalars(q))

    def has_history(self, universe: str) -> bool:
        """True if the universe has any closed membership intervals (i.e. real history, not a snapshot)."""
        with self.sf() as s:
            return s.scalar(select(m.UniverseMembership.id).where(
                m.UniverseMembership.universe == universe, m.UniverseMembership.end_date.is_not(None)).limit(1)) is not None

    # --- dataset versions -----------------------------------------------------
    def register_dataset(self, spec: dict, description: str = "") -> str:
        vid = hash_obj(spec, 32)
        with self.sf() as s, s.begin():
            if s.get(m.DatasetVersion, vid) is None:
                s.add(m.DatasetVersion(id=vid, spec=spec, description=description))
        return vid


class PointInTimeStore:
    """As-of queries for fundamentals, news and macro. Filters on `available_at`, never on period dates."""

    def __init__(self, sf: sessionmaker[Session]):
        self.sf = sf

    def fundamentals_asof(self, asset_id: int, asof) -> dict[str, float]:
        t = _naive_utc(asof)
        with self.sf() as s:
            rows = s.execute(select(m.Fundamental.metric, m.Fundamental.value, m.Fundamental.available_at)
                             .where(m.Fundamental.asset_id == asset_id, m.Fundamental.available_at <= t)
                             .order_by(m.Fundamental.available_at)).all()
        out: dict[str, float] = {}
        for metric, value, _ in rows:  # later availability overwrites earlier (restatements)
            out[metric] = value
        return out

    def news_asof(self, asset_id: int | None, asof, lookback: pd.Timedelta = pd.Timedelta(days=7)):
        t = _naive_utc(asof)
        with self.sf() as s:
            q = select(m.News).where(and_(m.News.available_at <= t, m.News.available_at > t - lookback))
            if asset_id is not None:
                q = q.where(m.News.asset_id == asset_id)
            return list(s.scalars(q.order_by(m.News.available_at)))

    def macro_asof(self, series: str, asof) -> float | None:
        t = _naive_utc(asof)
        with self.sf() as s:
            return s.scalar(select(m.MacroData.value)
                            .where(m.MacroData.series == series, m.MacroData.available_at <= t)
                            .order_by(m.MacroData.observation_date.desc(), m.MacroData.available_at.desc()).limit(1))
