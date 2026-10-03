"""Database schema.

Design notes:
- All timestamps are stored as timezone-naive UTC.
- Point-in-time correctness: fundamentals, news and macro rows carry `available_at`,
  the moment the information became public. Queries for a decision at time T must
  filter `available_at <= T` (see qsts.data.repository.PointInTimeStore).
- Universe membership is stored as intervals so historical universes can be rebuilt
  (survivorship-bias protection).
- Strategies are never deleted: status changes are appended to strategy_status_history.
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


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # content hash
    description: Mapped[str | None] = mapped_column(Text)
    spec: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


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
    analysis_ref: Mapped[int | None] = mapped_column(ForeignKey("ai_responses.id"))
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


class FeatureDefinition(Base):
    __tablename__ = "features"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # name@version hash
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(16))
    spec: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class IndicatorDefinition(Base):
    __tablename__ = "indicators"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict] = mapped_column(JSON)


class Strategy(Base):
    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    family: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="RESEARCH", index=True)
    origin: Mapped[str] = mapped_column(String(16), default="human")  # human | ai | evolution
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # content hash
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSON)
    parent_version_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),)


class StrategyStatusHistory(Base):
    __tablename__ = "strategy_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    strategy_version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"))
    kind: Mapped[str] = mapped_column(String(32))  # backtest | walk_forward | monte_carlo | oos ...
    config: Mapped[dict] = mapped_column(JSON)  # params, features, timeframes, periods, seed, costs
    seed: Mapped[int] = mapped_column(Integer)
    code_version: Mapped[str | None] = mapped_column(String(64))
    ai_provider: Mapped[str | None] = mapped_column(String(32))
    ai_model: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Backtest(Base):
    __tablename__ = "backtests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    metrics: Mapped[dict] = mapped_column(JSON)
    trades: Mapped[list] = mapped_column(JSON)
    equity_curve: Mapped[list] = mapped_column(JSON)


class WalkForwardRun(Base):
    __tablename__ = "walk_forward_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    fold: Mapped[int] = mapped_column(Integer)
    train_start: Mapped[datetime] = mapped_column(DateTime)
    train_end: Mapped[datetime] = mapped_column(DateTime)
    test_start: Mapped[datetime] = mapped_column(DateTime)
    test_end: Mapped[datetime] = mapped_column(DateTime)
    chosen_params: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON)


class MonteCarloRun(Base):
    __tablename__ = "monte_carlo_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    method: Mapped[str] = mapped_column(String(32))
    n_sims: Mapped[int] = mapped_column(Integer)
    percentiles: Mapped[dict] = mapped_column(JSON)


class OOSAccessLog(Base):
    """Every read of the reserved out-of-sample dataset is logged (and limited)."""
    __tablename__ = "oos_access_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_version_id: Mapped[str] = mapped_column(String(32), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    purpose: Mapped[str] = mapped_column(Text)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    asof: Mapped[datetime] = mapped_column(DateTime)  # data cut-off used for the decision
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    strategy_version_id: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # LONG | SHORT | NO_TRADE
    entry: Mapped[float | None] = mapped_column(Float)
    stop: Mapped[float | None] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)  # calibrated only; else NULL
    size: Mapped[float | None] = mapped_column(Float)
    reasons: Mapped[dict | None] = mapped_column(JSON)
    snapshot: Mapped[dict | None] = mapped_column(JSON)  # what the system saw


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)  # idempotency
    broker: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(16))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Fill(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    environment: Mapped[str] = mapped_column(String(16))
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    strategy_version_id: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    stop: Mapped[float | None] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (Index("ix_positions_open", "environment", "closed_at"),)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    environment: Mapped[str] = mapped_column(String(16))
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    exposure: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)
    detail: Mapped[dict | None] = mapped_column(JSON)


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    severity: Mapped[str] = mapped_column(String(16))
    rule: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON)


class AIRequest(Base):
    __tablename__ = "ai_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(32), index=True)  # cache key
    payload: Mapped[dict] = mapped_column(JSON)


class AIResponse(Base):
    __tablename__ = "ai_responses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("ai_requests.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    content: Mapped[dict] = mapped_column(JSON)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)


class SystemLog(Base):
    __tablename__ = "system_logs"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    level: Mapped[str] = mapped_column(String(8))
    component: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON)


class BrokerEvent(Base):
    __tablename__ = "broker_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    broker: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict | None] = mapped_column(JSON)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False)
