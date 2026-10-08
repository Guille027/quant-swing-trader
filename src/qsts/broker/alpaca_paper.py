"""Alpaca PAPER account through Alpaca's official Python SDK (`alpaca-py`, install with `.[alpaca]`).

The client is always created with `paper=True` (Alpaca's paper endpoint); there is no way to point it at a live
account from this app. The keys come from this computer's `.env` (QSTS_ALPACA_API_KEY / QSTS_ALPACA_SECRET_KEY) and
never leave it.

Behaviour relied on, as documented in the SDK (alpaca-py 0.44, `alpaca.trading.enums`):
- a `day` order submitted after the close is queued and submitted the following trading day;
- equities support the order classes simple, bracket (needs take_profit AND stop_loss), oco and oto (needs one of
  them); fractional quantities only with market orders, so this app always sends whole shares.
Not documented there, so handled defensively: exit legs of a bracket follow the parent's time in force (reported by
users on Alpaca's forum), so protective orders are re-checked every evening and re-sent if missing.
"""
from __future__ import annotations

from typing import Any


class BrokerError(RuntimeError):
    pass


def _f(x) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _v(x) -> Any:
    return getattr(x, "value", x)


def order_dict(o) -> dict:
    """A broker order as a plain dict (SDK model or already a dict)."""
    if isinstance(o, dict):
        return o
    return {"id": str(o.id), "client_order_id": o.client_order_id, "symbol": o.symbol, "side": _v(o.side),
            "qty": _f(o.qty), "filled_qty": _f(o.filled_qty), "filled_avg_price": _f(o.filled_avg_price),
            "status": _v(o.status), "type": _v(o.type or o.order_type), "order_class": _v(o.order_class),
            "time_in_force": _v(o.time_in_force), "stop_price": _f(o.stop_price), "limit_price": _f(o.limit_price),
            "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
            "filled_at": o.filled_at.isoformat() if o.filled_at else None,
            "legs": [order_dict(x) for x in (o.legs or [])]}


class AlpacaPaper:
    """Narrow interface used by the paper trader (a fake with the same methods is used in tests)."""
    name = "alpaca-paper"

    def __init__(self, api_key: str, secret_key: str):
        if not api_key or not secret_key:
            raise BrokerError("faltan las claves de Alpaca")
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as e:
            raise BrokerError("falta el componente de Alpaca: ejecuta 'Actualizar QSTS.bat'") from e
        self.client = TradingClient(api_key, secret_key, paper=True)  # PAPER ONLY, by design

    def _call(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001 - network / API errors are reported, never hidden
            msg = str(e)
            raise BrokerError(msg[:300] or repr(e)) from e

    def account(self) -> dict:
        a = self._call(self.client.get_account)
        return {"equity": _f(a.equity), "cash": _f(a.cash), "buying_power": _f(a.buying_power),
                "status": _v(a.status), "currency": a.currency, "shorting_enabled": bool(a.shorting_enabled),
                "trading_blocked": bool(a.trading_blocked), "account_blocked": bool(a.account_blocked),
                "account_number": a.account_number}

    def clock(self) -> dict:
        c = self._call(self.client.get_clock)
        return {"is_open": bool(c.is_open), "next_open": c.next_open.isoformat(), "next_close": c.next_close.isoformat(),
                "timestamp": c.timestamp.isoformat()}

    def positions(self) -> dict[str, dict]:
        out = {}
        for p in self._call(self.client.get_all_positions):
            q = _f(p.qty) or 0.0
            out[p.symbol] = {"qty": q if _v(p.side) == "long" else -abs(q), "avg_entry_price": _f(p.avg_entry_price),
                             "current_price": _f(p.current_price), "unrealized_pl": _f(p.unrealized_pl)}
        return out

    def asset(self, symbol: str) -> dict:
        a = self._call(self.client.get_asset, symbol)
        return {"tradable": bool(a.tradable), "shortable": bool(a.shortable), "easy_to_borrow": bool(a.easy_to_borrow),
                "fractionable": bool(a.fractionable)}

    def submit(self, req: dict) -> dict:
        """`req`: symbol, qty (whole shares), side (buy|sell), type (market|stop|limit), time_in_force (day|gtc),
        client_order_id, optional order_class (bracket|oco|oto), stop_price, limit_price, take_profit, stop_loss."""
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (LimitOrderRequest, MarketOrderRequest, StopLossRequest,
                                             StopOrderRequest, TakeProfitRequest)
        common = {"symbol": req["symbol"], "qty": int(req["qty"]), "side": OrderSide(req["side"]),
                  "time_in_force": TimeInForce(req.get("time_in_force", "day")),
                  "client_order_id": req["client_order_id"]}
        if req.get("order_class"):
            common["order_class"] = OrderClass(req["order_class"])
        if req.get("take_profit") is not None:
            common["take_profit"] = TakeProfitRequest(limit_price=round(float(req["take_profit"]), 2))
        if req.get("stop_loss") is not None:
            common["stop_loss"] = StopLossRequest(stop_price=round(float(req["stop_loss"]), 2))
        kind = req.get("type", "market")
        try:
            if kind == "market":
                r = MarketOrderRequest(**common)
            elif kind == "stop":
                r = StopOrderRequest(stop_price=round(float(req["stop_price"]), 2), **common)
            elif kind == "limit":
                r = LimitOrderRequest(limit_price=round(float(req["limit_price"]), 2), **common)
            else:
                raise BrokerError(f"tipo de orden no soportado: {kind}")
        except ValueError as e:  # the SDK validates the request before sending it
            raise BrokerError(f"orden no válida: {e}") from e
        return order_dict(self._call(self.client.submit_order, r))

    def get_order(self, broker_id: str) -> dict:
        from alpaca.trading.requests import GetOrderByIdRequest
        return order_dict(self._call(self.client.get_order_by_id, broker_id, GetOrderByIdRequest(nested=True)))

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500,
                               symbols=[symbol] if symbol else None)
        return [order_dict(o) for o in self._call(self.client.get_orders, req)]

    def cancel(self, broker_id: str) -> None:
        self._call(self.client.cancel_order_by_id, broker_id)
