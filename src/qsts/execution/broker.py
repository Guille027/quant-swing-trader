"""BrokerAdapter abstraction + PaperBroker.

The trading logic never depends on a concrete broker. Going PAPER -> LIVE means swapping the
adapter, nothing else. PaperBroker uses the SAME fill model as the backtest engine (CostModel:
half-spread + slippage + commission, volume-participation partial fills, gap handling, next-bar
latency) so paper results are comparable with research results.
"""
from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from qsts.backtest.engine import CostModel


class BrokerError(RuntimeError):
    pass


class BrokerUnavailable(BrokerError):
    pass


class OrderRejected(BrokerError):
    pass


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    tradable: bool
    shortable: bool | None  # None = broker does not report it -> treat as not shortable
    fractional: bool
    min_qty: float
    currency: str = "USD"


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str  # idempotency key: resubmitting the same id never creates a 2nd order
    symbol: str
    side: Side
    qty: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None


@dataclass
class Fill:
    ts: pd.Timestamp
    qty: float
    price: float
    commission: float


@dataclass
class OrderState:
    request: OrderRequest
    broker_order_id: str
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    reject_reason: str | None = None

    @property
    def remaining(self) -> float:
        return self.request.qty - self.filled_qty


@dataclass(frozen=True)
class AccountInfo:
    cash: float
    equity: float
    currency: str


class BrokerAdapter(ABC):
    name: str
    environment: str  # "paper" | "live"

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def account(self) -> AccountInfo: ...

    @abstractmethod
    def positions(self) -> dict[str, float]:
        """symbol -> signed quantity (negative = short)."""

    @abstractmethod
    def instrument(self, symbol: str) -> InstrumentInfo: ...

    @abstractmethod
    def submit(self, req: OrderRequest) -> OrderState: ...

    @abstractmethod
    def cancel(self, client_order_id: str) -> OrderState: ...

    @abstractmethod
    def orders(self) -> dict[str, OrderState]: ...


class PaperBroker(BrokerAdapter):
    name = "paper"
    environment = "paper"

    def __init__(self, cash: float, costs: CostModel = CostModel(), instruments: dict[str, InstrumentInfo] | None = None,
                 currency: str = "USD"):
        self._cash = cash
        self.costs = costs
        self._pos: dict[str, float] = {}
        self._avg: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._orders: dict[str, OrderState] = {}
        self._instruments = instruments or {}
        self._ids = itertools.count(1)
        self._connected = True
        self.currency = currency

    # -- connectivity simulation (for failure tests) --------------------------------
    def set_connected(self, ok: bool) -> None:
        self._connected = ok

    def _check(self):
        if not self._connected:
            raise BrokerUnavailable("paper broker disconnected (simulated)")

    def is_connected(self) -> bool:
        return self._connected

    def account(self) -> AccountInfo:
        self._check()
        eq = self._cash + sum(q * self._last.get(s, self._avg.get(s, 0)) for s, q in self._pos.items())
        return AccountInfo(self._cash, eq, self.currency)

    def positions(self) -> dict[str, float]:
        self._check()
        return {s: q for s, q in self._pos.items() if abs(q) > 1e-12}

    def instrument(self, symbol: str) -> InstrumentInfo:
        self._check()
        return self._instruments.get(symbol, InstrumentInfo(symbol, True, None, True, 1e-6))

    def orders(self) -> dict[str, OrderState]:
        self._check()
        return dict(self._orders)

    def submit(self, req: OrderRequest) -> OrderState:
        self._check()
        if req.client_order_id in self._orders:
            return self._orders[req.client_order_id]  # idempotent: duplicate submission is a no-op
        st = OrderState(req, f"P{next(self._ids)}")
        info = self.instrument(req.symbol)
        reason = None
        if not info.tradable:
            reason = "instrument not tradable"
        elif req.qty <= 0 or req.qty < info.min_qty:
            reason = f"qty {req.qty} below minimum {info.min_qty}"
        elif not info.fractional and not float(req.qty).is_integer():
            reason = "fractional shares not supported"
        elif req.side is Side.SELL and self._pos.get(req.symbol, 0) - req.qty < -1e-12 and info.shortable is not True:
            reason = "short selling not available for instrument"
        elif req.order_type is OrderType.LIMIT and req.limit_price is None:
            reason = "limit order without limit price"
        elif req.order_type is OrderType.STOP and req.stop_price is None:
            reason = "stop order without stop price"
        if reason:
            st.status, st.reject_reason = OrderStatus.REJECTED, reason
        self._orders[req.client_order_id] = st
        return st

    def cancel(self, client_order_id: str) -> OrderState:
        self._check()
        st = self._orders[client_order_id]
        if st.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            st.status = OrderStatus.CANCELLED
        return st

    # -- market simulation ------------------------------------------------------------
    def on_bar(self, symbol: str, ts: pd.Timestamp, o: float, h: float, l: float, c: float, v: float) -> list[Fill]:
        """Process open orders for `symbol` against a NEW bar. Orders submitted before this bar
        are eligible (latency = one bar). Market: open. Stop: trigger at stop or gap open.
        Limit: at limit or better open. Volume cap -> partial fills (rest stays working)."""
        fills = []
        cap = v * self.costs.max_volume_participation if v > 0 else 0.0
        for st in self._orders.values():
            r = st.request
            if r.symbol != symbol or st.status not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                continue
            sgn = 1 if r.side is Side.BUY else -1
            ref = None
            if r.order_type is OrderType.MARKET:
                ref = o
            elif r.order_type is OrderType.STOP:
                if sgn > 0 and h >= r.stop_price:
                    ref = max(o, r.stop_price)
                elif sgn < 0 and l <= r.stop_price:
                    ref = min(o, r.stop_price)
            else:
                if sgn > 0 and l <= r.limit_price:
                    ref = min(o, r.limit_price)
                elif sgn < 0 and h >= r.limit_price:
                    ref = max(o, r.limit_price)
            if ref is None:
                continue
            qty = min(st.remaining, cap)
            if not self.instrument(symbol).fractional:
                qty = float(np.floor(qty))
            if qty <= 0:
                continue
            px = self.costs.fill_price(ref, sgn) if r.order_type is not OrderType.LIMIT else ref
            comm = self.costs.commission(qty, px)
            if sgn > 0 and qty * px + comm > self._cash + 1e-9 and self._pos.get(symbol, 0) >= 0:
                st.status, st.reject_reason = OrderStatus.REJECTED, "insufficient cash"
                continue
            self._apply(symbol, sgn * qty, px, comm)
            f = Fill(ts, qty, px, comm)
            st.fills.append(f)
            st.filled_qty += qty
            st.status = OrderStatus.FILLED if st.remaining <= 1e-12 else OrderStatus.PARTIALLY_FILLED
            fills.append(f)
        self._last[symbol] = c
        return fills

    def _apply(self, symbol, signed_qty, px, comm):
        self._cash -= signed_qty * px + comm
        q0 = self._pos.get(symbol, 0.0)
        q1 = q0 + signed_qty
        if q0 == 0 or np.sign(q0) == np.sign(signed_qty):
            self._avg[symbol] = (self._avg.get(symbol, 0) * abs(q0) + px * abs(signed_qty)) / abs(q1)
        elif np.sign(q1) != np.sign(q0) and q1 != 0:
            self._avg[symbol] = px
        self._pos[symbol] = q1
