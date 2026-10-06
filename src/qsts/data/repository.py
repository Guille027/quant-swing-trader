"""Persistence for market data. Only `ValidatedBars` can be stored. Timestamps are stored as naive UTC."""
from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
from sqlalchemy import and_, delete, func, or_, select

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

    def data_token(self, symbol: str, timeframe: Timeframe = Timeframe.D1) -> tuple:
        """Cheap fingerprint of everything stored for a symbol (bars, corporate actions, earnings): it changes
        whenever any of them is added or re-downloaded. Used to cache prepared research frames safely."""
        try:
            aid = self.asset_id(symbol)
        except KeyError:
            return ("missing",)
        with self.sf() as s:
            p = s.execute(select(func.count(), func.max(m.Price.ts), func.max(m.Price.ingested_at))
                          .where(m.Price.asset_id == aid, m.Price.timeframe == Timeframe(timeframe).value)).one()
            a = s.execute(select(func.count(), func.max(m.CorporateAction.ex_date), func.sum(m.CorporateAction.value))
                          .where(m.CorporateAction.asset_id == aid)).one()
            e = s.execute(select(func.count(), func.max(m.EarningsEvent.fetched_at))
                          .where(m.EarningsEvent.asset_id == aid)).one()
        return tuple(str(x) for x in (*p, *a, *e))

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
        # naive UTC datetimes -> datetime64 in one vectorised step (pd.to_datetime on objects iterates in Python)
        idx = pd.DatetimeIndex(np.array(df["ts"].to_numpy(), dtype="datetime64[ns]")) if len(df) else \
            pd.DatetimeIndex(pd.to_datetime(df["ts"])).as_unit("ns")
        df.index = (idx.tz_localize("UTC") if idx.tz is None else idx).rename("ts")
        return df.drop(columns="ts").astype({c: "float64" for c in OHLCV})

    def last_bar(self, symbol: str, timeframe: Timeframe = Timeframe.D1) -> pd.Timestamp | None:
        try:
            aid = self.asset_id(symbol)
        except KeyError:
            return None
        with self.sf() as s:
            t = s.scalar(select(func.max(m.Price.ts)).where(m.Price.asset_id == aid,
                                                             m.Price.timeframe == Timeframe(timeframe).value))
        return None if t is None else pd.Timestamp(t).tz_localize("UTC")

    def summary(self, timeframe: Timeframe = Timeframe.D1) -> list[dict]:
        """One row per symbol with stored bars: name, sector, first/last bar, number of bars."""
        with self.sf() as s:
            rows = s.execute(select(m.Asset.symbol, m.Asset.name, m.Asset.sector, func.min(m.Price.ts),
                                    func.max(m.Price.ts), func.count())
                             .join(m.Price, m.Price.asset_id == m.Asset.id)
                             .where(m.Price.timeframe == Timeframe(timeframe).value)
                             .group_by(m.Asset.id).order_by(m.Asset.symbol)).all()
        return [{"symbol": r[0], "name": r[1], "sector": r[2], "first": str(pd.Timestamp(r[3]).date()),
                 "last": str(pd.Timestamp(r[4]).date()), "bars": int(r[5])} for r in rows]

    def coverage(self, timeframe: Timeframe) -> dict[str, dict]:
        """Per symbol with bars of `timeframe`: first/last bar and count (index lookups per asset, no full scan)."""
        with self.sf() as s:
            ids = dict(s.execute(select(m.Asset.id, m.Asset.symbol)).all())
            if not ids:
                return {}
            rows = s.execute(select(m.Price.asset_id, func.min(m.Price.ts), func.max(m.Price.ts), func.count())
                             .where(m.Price.asset_id.in_(list(ids)), m.Price.timeframe == Timeframe(timeframe).value)
                             .group_by(m.Price.asset_id)).all()
        return {ids[a]: {"first": pd.Timestamp(f).tz_localize("UTC"), "last": pd.Timestamp(la).tz_localize("UTC"),
                         "bars": int(n)} for a, f, la, n in rows}

    def liquid_symbols(self, n: int, days: int = 90) -> list[str]:
        """The `n` symbols with the highest average traded value (close x volume) over the last `days` days of the
        stored daily data; only symbols still trading (last bar within 10 days of the newest one)."""
        with self.sf() as s:
            ids = dict(s.execute(select(m.Asset.id, m.Asset.symbol)).all())
            if not ids:
                return []
            last = dict(s.execute(select(m.Price.asset_id, func.max(m.Price.ts))
                                  .where(m.Price.asset_id.in_(list(ids)), m.Price.timeframe == Timeframe.D1.value)
                                  .group_by(m.Price.asset_id)).all())
            if not last:
                return []
            newest = max(last.values())
            alive = [a for a, t in last.items() if t >= newest - pd.Timedelta(days=10)]
            rows = s.execute(select(m.Price.asset_id, func.avg(m.Price.close * m.Price.volume), func.count())
                             .where(m.Price.asset_id.in_(alive), m.Price.timeframe == Timeframe.D1.value,
                                    m.Price.ts >= newest - pd.Timedelta(days=days))
                             .group_by(m.Price.asset_id)).all()
        ranked = sorted((r for r in rows if r[2] >= 20 and r[1]), key=lambda r: -r[1])
        return [ids[r[0]] for r in ranked[:n]]

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

    # ------------------------------------------------------------------ earnings
    def store_earnings(self, symbol: str, events: pd.DataFrame, source: str) -> int:
        """Upsert by (asset, announcement time): later fetches fill in reported EPS / surprise."""
        aid = self.asset_id(symbol)
        rows = []
        for r in events.itertuples(index=False):
            def num(x):
                return None if x is None or pd.isna(x) else float(x)
            rows.append({"asset_id": aid, "announced_at": _naive_utc(r.announced_at),
                         "time_known": bool(getattr(r, "time_known", True)), "eps_estimate": num(getattr(r, "eps_estimate", None)),
                         "eps_reported": num(getattr(r, "eps_reported", None)), "surprise_pct": num(getattr(r, "surprise_pct", None)),
                         "source": source, "fetched_at": _naive_utc(pd.Timestamp.now(tz="UTC"))})
        if rows:
            with self.sf() as s, s.begin():
                _upsert(s, m.EarningsEvent, rows, ["asset_id", "announced_at"],
                        ["time_known", "eps_estimate", "eps_reported", "surprise_pct", "source", "fetched_at"])
        return len(rows)

    # ---------------------------------------------------------------- exchange rates (display of EUR accounts)
    def store_fx(self, series: str, rates: pd.Series, source: str) -> int:
        """Daily closes of an exchange rate (e.g. EURUSD = USD per EUR), keyed by date. Re-fetches overwrite."""
        rates = pd.Series(rates).dropna()
        if rates.empty:
            return 0
        now = _naive_utc(pd.Timestamp.now(tz="UTC"))
        days = [pd.Timestamp(d).date() for d in rates.index]
        with self.sf() as s, s.begin():
            s.execute(delete(m.MacroData).where(m.MacroData.series == series, m.MacroData.observation_date.in_(days)))
            s.add_all([m.MacroData(series=series, observation_date=d, value=float(v), available_at=now, source=source)
                       for d, v in zip(days, rates.to_numpy())])
        return len(days)

    def fx_series(self, series: str) -> pd.Series:
        with self.sf() as s:
            rows = s.execute(select(m.MacroData.observation_date, m.MacroData.value)
                             .where(m.MacroData.series == series).order_by(m.MacroData.observation_date)).all()
        if not rows:
            return pd.Series(dtype=float)
        return pd.Series([r[1] for r in rows], index=pd.DatetimeIndex([pd.Timestamp(r[0], tz="UTC") for r in rows]))

    def load_earnings(self, symbol: str) -> pd.DataFrame:
        cols = ["announced_at", "time_known", "eps_estimate", "eps_reported", "surprise_pct"]
        try:
            aid = self.asset_id(symbol)
        except KeyError:
            return pd.DataFrame(columns=cols)
        with self.sf() as s:
            rows = s.execute(select(m.EarningsEvent.announced_at, m.EarningsEvent.time_known, m.EarningsEvent.eps_estimate,
                                    m.EarningsEvent.eps_reported, m.EarningsEvent.surprise_pct)
                             .where(m.EarningsEvent.asset_id == aid).order_by(m.EarningsEvent.announced_at)).all()
        df = pd.DataFrame(rows, columns=cols)
        if len(df):
            df["announced_at"] = pd.to_datetime(df["announced_at"]).dt.tz_localize("UTC")
        return df

    def earnings_summary(self) -> dict[str, dict]:
        with self.sf() as s:
            rows = s.execute(select(m.Asset.symbol, func.count(), func.min(m.EarningsEvent.announced_at),
                                    func.max(m.EarningsEvent.announced_at), func.count(m.EarningsEvent.surprise_pct))
                             .join(m.EarningsEvent, m.EarningsEvent.asset_id == m.Asset.id).group_by(m.Asset.symbol)).all()
        return {r[0]: {"events": int(r[1]), "first": str(pd.Timestamp(r[2]).date()), "last": str(pd.Timestamp(r[3]).date()),
                       "with_surprise": int(r[4])} for r in rows}

    # ------------------------------------------------------------------ universe (survivorship-safe)
    def add_membership(self, universe: str, symbol: str, start: date, end: date | None = None,
                       source: str | None = None) -> None:
        aid = self.asset_id(symbol)
        with self.sf() as s, s.begin():
            s.add(m.UniverseMembership(universe=universe, asset_id=aid, start_date=start, end_date=end, source=source))

    def set_memberships(self, universe: str, rows: list[tuple[str, date, date | None]], source: str) -> int:
        """Replace this source's membership rows for `universe` (idempotent re-import of a member list)."""
        with self.sf() as s, s.begin():
            s.execute(delete(m.UniverseMembership).where(m.UniverseMembership.universe == universe,
                                                         m.UniverseMembership.source == source))
            ids = dict(s.execute(select(m.Asset.symbol, m.Asset.id)).all())
            n = 0
            for sym, start, end in rows:
                if sym in ids:
                    s.add(m.UniverseMembership(universe=universe, asset_id=ids[sym], start_date=start, end_date=end,
                                               source=source))
                    n += 1
        return n

    def membership_starts(self, universe: str) -> dict[str, date]:
        """Earliest known membership start per symbol."""
        with self.sf() as s:
            rows = s.execute(select(m.Asset.symbol, func.min(m.UniverseMembership.start_date))
                             .join(m.UniverseMembership, m.UniverseMembership.asset_id == m.Asset.id)
                             .where(m.UniverseMembership.universe == universe).group_by(m.Asset.symbol)).all()
        return {sym: d for sym, d in rows if d is not None}

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
