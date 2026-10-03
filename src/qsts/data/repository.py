"""Persistence for market data. Only `ValidatedBars` can be stored. Timestamps are stored as naive UTC."""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
from sqlalchemy import and_, delete, or_, select

from qsts.data.bars import OHLCV, Timeframe
from qsts.data.quality import ValidatedBars
from qsts.db import models as m

_CHUNK = 500


def _naive_utc(t) -> datetime:
    ts = pd.Timestamp(t)
    if ts.tz is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.to_pydatetime()


def _upsert(session, model, rows: list[dict], keys: list[str], update: list[str]) -> None:
    dialect = session.get_bind().dialect.name
    if dialect in ("sqlite", "postgresql"):
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        for i in range(0, len(rows), _CHUNK):
            stmt = insert(model).values(rows[i:i + _CHUNK])
            stmt = stmt.on_conflict_do_update(index_elements=keys, set_={c: stmt.excluded[c] for c in update})
            session.execute(stmt)
        return
    for r in rows:  # portable fallback
        session.execute(delete(model).where(and_(*[getattr(model, k) == r[k] for k in keys])))
        session.add(model(**r))


class MarketDataRepository:
    def __init__(self, sf):
        self.sf = sf

    # ------------------------------------------------------------------ assets
    def upsert_asset(self, symbol: str, exchange: str | None = None, **fields) -> int:
        symbol = symbol.upper()
        with self.sf() as s, s.begin():
            q = select(m.Asset).where(m.Asset.symbol == symbol)
            q = q.where(m.Asset.exchange.is_(None) if exchange is None else m.Asset.exchange == exchange)
            a = s.scalars(q).first()
            if a is None:
                a = m.Asset(symbol=symbol, exchange=exchange)
                s.add(a)
            for k, v in fields.items():
                if v is not None:
                    setattr(a, k, v)
            s.flush()
            return a.id

    def asset_id(self, symbol: str) -> int:
        with self.sf() as s:
            aid = s.scalars(select(m.Asset.id).where(m.Asset.symbol == symbol.upper()).order_by(m.Asset.id)).first()
        if aid is None:
            raise KeyError(f"unknown asset {symbol}")
        return aid

    # ------------------------------------------------------------------ bars
    def store_bars(self, vb: ValidatedBars, source: str) -> int:
        if not isinstance(vb, ValidatedBars):
            raise TypeError("only ValidatedBars can be stored (run qsts.data.quality.validate_and_clean first)")
        aid = self.asset_id(vb.symbol)
        df = vb.df
        rows = [{"asset_id": aid, "timeframe": vb.timeframe.value, "ts": _naive_utc(t), "open": float(o),
                 "high": float(h), "low": float(lo), "close": float(c), "volume": float(v), "adj_factor": 1.0,
                 "source": source}
                for t, o, h, lo, c, v in zip(df.index, df["open"], df["high"], df["low"], df["close"], df["volume"])]
        with self.sf() as s, s.begin():
            _upsert(s, m.Price, rows, ["asset_id", "timeframe", "ts"],
                    ["open", "high", "low", "close", "volume", "adj_factor", "source"])
        return len(rows)

    def load_bars(self, symbol: str, timeframe: Timeframe = Timeframe.D1, start=None, end=None) -> pd.DataFrame:
        aid = self.asset_id(symbol)
        q = select(m.Price.ts, m.Price.open, m.Price.high, m.Price.low, m.Price.close, m.Price.volume,
                   m.Price.source).where(m.Price.asset_id == aid, m.Price.timeframe == Timeframe(timeframe).value)
        if start is not None:
            q = q.where(m.Price.ts >= _naive_utc(start))
        if end is not None:
            q = q.where(m.Price.ts <= _naive_utc(end))
        with self.sf() as s:
            rows = s.execute(q.order_by(m.Price.ts)).all()
        df = pd.DataFrame(rows, columns=["ts", *OHLCV, "source"])
        idx = pd.DatetimeIndex(pd.to_datetime(df["ts"])).as_unit("ns")
        df.index = (idx.tz_localize("UTC") if idx.tz is None else idx).rename("ts")
        return df.drop(columns="ts").astype({c: "float64" for c in OHLCV})

    # ------------------------------------------------------------------ corporate actions
    def store_corporate_actions(self, symbol: str, actions: pd.DataFrame, source: str) -> int:
        aid = self.asset_id(symbol)
        rows = [{"asset_id": aid, "ex_date": pd.Timestamp(r.ex_date).date(), "kind": r.kind, "value": float(r.value),
                 "source": source} for r in actions.itertuples(index=False)]
        if rows:
            with self.sf() as s, s.begin():
                _upsert(s, m.CorporateAction, rows, ["asset_id", "ex_date", "kind"], ["value", "source"])
        return len(rows)

    def load_corporate_actions(self, symbol: str) -> pd.DataFrame:
        aid = self.asset_id(symbol)
        with self.sf() as s:
            rows = s.execute(select(m.CorporateAction.ex_date, m.CorporateAction.kind, m.CorporateAction.value)
                             .where(m.CorporateAction.asset_id == aid).order_by(m.CorporateAction.ex_date)).all()
        df = pd.DataFrame(rows, columns=["ex_date", "kind", "value"])
        df["ex_date"] = pd.to_datetime(df["ex_date"]).dt.tz_localize("UTC")
        return df

    # ------------------------------------------------------------------ universe (survivorship-safe)
    def add_membership(self, universe: str, symbol: str, start: date, end: date | None = None,
                       source: str | None = None) -> None:
        aid = self.asset_id(symbol)
        with self.sf() as s, s.begin():
            s.add(m.UniverseMembership(universe=universe, asset_id=aid, start_date=start, end_date=end, source=source))

    def universe_asof(self, universe: str, d: date) -> list[str]:
        """Members on date d: start_date <= d < end_date (end exclusive)."""
        with self.sf() as s:
            q = (select(m.Asset.symbol).join(m.UniverseMembership, m.UniverseMembership.asset_id == m.Asset.id)
                 .where(m.UniverseMembership.universe == universe, m.UniverseMembership.start_date <= d,
                        or_(m.UniverseMembership.end_date.is_(None), m.UniverseMembership.end_date > d))
                 .distinct().order_by(m.Asset.symbol))
            return list(s.scalars(q))

    def has_history(self, universe: str) -> bool:
        with self.sf() as s:
            return s.scalars(select(m.UniverseMembership.id).where(m.UniverseMembership.universe == universe)
                             .limit(1)).first() is not None


class PointInTimeStore:
    """As-of queries for fundamentals / news / macro: only rows with available_at <= t are visible."""

    def __init__(self, sf):
        self.sf = sf

    def fundamentals_asof(self, asset_id: int, t) -> dict[str, float | None]:
        tt = _naive_utc(t)
        with self.sf() as s:
            rows = s.execute(select(m.Fundamental.metric, m.Fundamental.value, m.Fundamental.period_end,
                                    m.Fundamental.available_at)
                             .where(m.Fundamental.asset_id == asset_id, m.Fundamental.available_at <= tt)).all()
        best: dict[str, tuple] = {}
        for metric, value, pe, av in rows:  # latest period; for restatements the latest publication wins
            if metric not in best or (pe, av) > best[metric][:2]:
                best[metric] = (pe, av, value)
        return {k: v[2] for k, v in best.items()}

    def news_asof(self, asset_id: int, t, since=None) -> list[dict]:
        tt = _naive_utc(t)
        q = select(m.News).where(m.News.asset_id == asset_id, m.News.available_at <= tt)
        if since is not None:
            q = q.where(m.News.available_at >= _naive_utc(since))
        with self.sf() as s:
            return [{"published_at": n.published_at, "available_at": n.available_at, "source": n.source,
                     "headline": n.headline, "event_type": n.event_type, "sentiment": n.sentiment}
                    for n in s.scalars(q.order_by(m.News.available_at))]

    def macro_asof(self, series: str, t) -> float | None:
        tt = _naive_utc(t)
        with self.sf() as s:
            row = s.execute(select(m.MacroData.value).where(m.MacroData.series == series, m.MacroData.available_at <= tt)
                            .order_by(m.MacroData.observation_date.desc(), m.MacroData.available_at.desc())
                            .limit(1)).first()
        return None if row is None else row[0]
