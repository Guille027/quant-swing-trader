"""Database schema.

Design notes:
- All timestamps are stored as timezone-naive UTC.
- Point-in-time correctness: fundamentals, news and macro rows carry `available_at`,
  the moment the information became public. Queries for a decision at time T must
  filter `available_at <= T` (see qsts.data.repository.PointInTimeStore).
- Universe membership is stored as intervals so historical universes can be rebuilt
  (survivorship-bias protection).
- Lab: bots (a strategy on one stock), the orders sent to the Alpaca paper account and what the trader did.
  Tables of earlier versions of the app stay in old databases untouched; nothing reads them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    exchange: Mapped[str | None] = mapped_column(String(32))
    sector: Mapped[str | None] = mapped_column(String(64), index=True)
    industry: Mapped[str | None] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    listed_on: Mapped[datetime | None] = mapped_column(Date)
    delisted_on: Mapped[datetime | None] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("symbol", "exchange", name="uq_asset_symbol_exchange"),)


class UniverseMembership(Base):
    __tablename__ = "universe_membership"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    universe: Mapped[str] = mapped_column(String(32))
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    start_date: Mapped[datetime] = mapped_column(Date)
    end_date: Mapped[datetime | None] = mapped_column(Date)  # None = still member
    source: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (Index("ix_universe_dates", "universe", "start_date", "end_date"),)


class Price(Base):
    __tablename__ = "prices"
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, primary_key=True)  # bar OPEN time, UTC
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    adj_factor: Mapped[float] = mapped_column(Float, default=1.0)  # multiply raw OHLC to adjust
    source: Mapped[str] = mapped_column(String(32))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    ex_date: Mapped[datetime] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))  # split | dividend
    value: Mapped[float] = mapped_column(Float)  # split ratio or cash dividend per share
    source: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("asset_id", "ex_date", "kind", name="uq_corp_action"),)


class EarningsEvent(Base):
    """Quarterly results: announcement time (UTC) and EPS figures. Point-in-time use goes through
    qsts.data.earnings (info / impact sessions), never through the raw timestamp."""
    __tablename__ = "earnings_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    announced_at: Mapped[datetime] = mapped_column(DateTime)
    time_known: Mapped[bool] = mapped_column(Boolean, default=True)
    eps_estimate: Mapped[float | None] = mapped_column(Float)
    eps_reported: Mapped[float | None] = mapped_column(Float)
    surprise_pct: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("asset_id", "announced_at", name="uq_earnings_event"),)


class Fundamental(Base):
    __tablename__ = "fundamentals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    period_end: Mapped[datetime] = mapped_column(Date)
    metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[datetime] = mapped_column(DateTime)  # filing / publication time
    source: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (Index("ix_fund_pit", "asset_id", "metric", "available_at"),)


class News(Base):
    __tablename__ = "news"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"))
    published_at: Mapped[datetime] = mapped_column(DateTime)
    available_at: Mapped[datetime] = mapped_column(DateTime)  # when WE could have seen it
    source: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str | None] = mapped_column(String(32))
    headline: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[float | None] = mapped_column(Float)
    importance: Mapped[float | None] = mapped_column(Float)
    relevance: Mapped[float | None] = mapped_column(Float)
    analysis_ref: Mapped[int | None] = mapped_column(Integer)  # (an AI analysis in earlier versions)
    __table_args__ = (Index("ix_news_pit", "asset_id", "available_at"),)


class MacroData(Base):
    __tablename__ = "macro_data"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series: Mapped[str] = mapped_column(String(64))
    observation_date: Mapped[datetime] = mapped_column(Date)
    value: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[datetime] = mapped_column(DateTime)  # release time (vintages)
    source: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (Index("ix_macro_pit", "series", "available_at"),)


class LabBot(Base):
    """A strategy applied to one stock (one row of the library). Paper trading state lives here too."""
    __tablename__ = "lab_bots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # overrides of the strategy's published defaults
    size_pct: Mapped[float] = mapped_column(Float, default=100.0)  # backtest: % of the bot's equity per position
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    paper_status: Mapped[str] = mapped_column(String(16), default="off")  # off | active | stopped
    allocation_pct: Mapped[float | None] = mapped_column(Float)  # share of the Alpaca paper account
    capital: Mapped[float | None] = mapped_column(Float)  # dollars assigned when activated
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)  # incubation date: live results start here
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_signal_day: Mapped[datetime | None] = mapped_column(Date)  # last close whose signal was handled
    stop_level: Mapped[float | None] = mapped_column(Float)    # protective stop of the open paper position
    target_level: Mapped[float | None] = mapped_column(Float)  # profit target of the open paper position
    pending: Mapped[dict | None] = mapped_column(JSON)  # an entry waiting for the next open (after a reversal...)


class LabOrder(Base):
    """Every order the app sent to the Alpaca paper account (and what happened to it)."""
    __tablename__ = "lab_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # our client_order_id
    bot_id: Mapped[str] = mapped_column(ForeignKey("lab_bots.id"), index=True)
    broker_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))     # buy | sell
    qty: Mapped[float] = mapped_column(Float)
    purpose: Mapped[str] = mapped_column(String(16))  # entry | exit | stop | target | protect
    order_type: Mapped[str] = mapped_column(String(16))  # market | bracket | oco | stop | limit
    stop_price: Mapped[float | None] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    signal_day: Mapped[datetime | None] = mapped_column(Date)  # the close whose signal it executes
    status: Mapped[str] = mapped_column(String(24), default="submitted")
    filled_qty: Mapped[float | None] = mapped_column(Float)
    filled_price: Mapped[float | None] = mapped_column(Float)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSON)


class LabEvent(Base):
    """What the paper trader did and why (shown in the app; also the source of Telegram messages)."""
    __tablename__ = "lab_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    bot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # signal | order | fill | skip | error | info
    text: Mapped[str] = mapped_column(Text)


