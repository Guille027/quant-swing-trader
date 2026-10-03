# Trading 212 Public API — what the official docs say

Source: official OpenAPI description downloaded from <https://docs.trading212.com/api>
(`/_bundle/api.json`), fetched **2026-10-03**. API version `v0`, status **Beta** ("under active development").
Everything below is from that document unless marked *Not documented*. Re-check before implementing:
it is beta and may change.

## Scope
- Only **Invest** and **Stocks ISA** accounts. (CFD accounts are not mentioned.)
- Two environments with separate keys: **demo/paper** `https://demo.trading212.com/api/v0`,
  **live** `https://live.trading212.com/api/v0`.
- Orders execute only in the account's **primary currency**; multi-currency accounts are not supported via API.
- Orders by **quantity only** ("placing orders by value is not currently supported").

## Authentication
- HTTP **Basic**: API key as username, API secret as password (`Authorization: Basic base64(key:secret)`).
  A legacy `Authorization: <key>` header scheme is also listed.
- Keys can optionally be restricted to IP addresses (configured in the Trading 212 app).

## Endpoints (all under `/api/v0/equity`)
| Area | Endpoint | Rate limit |
|---|---|---|
| Account | `GET /account/summary` (cash available/reserved, investments, total value) | 1 / 5 s |
| Instruments | `GET /metadata/instruments` (ticker e.g. `AAPL_US_EQ`, ISIN, type, currency, `maxOpenQuantity`, `extendedHours`) | 1 / 50 s |
| Exchanges | `GET /metadata/exchanges` (working schedules: OPEN/CLOSE/PRE_MARKET/AFTER_HOURS events) | 1 / 30 s |
| Positions | `GET /positions` (qty, qty available for trading, avg price, current price, wallet impact) | 1 / 1 s |
| Pending orders | `GET /orders`, `GET /orders/{id}` | 1 / 5 s, 1 / 1 s |
| Place | `POST /orders/market` (`ticker`, `quantity`, `extendedHours`) | 50 / min |
| Place | `POST /orders/limit`, `/orders/stop`, `/orders/stop_limit` (+ `timeValidity` `DAY` \| `GOOD_TILL_CANCEL`) | 1 / 2 s each |
| Cancel | `DELETE /orders/{id}` (acceptance ≠ guaranteed cancel) | 50 / min |
| History | `GET /history/orders`, `/history/dividends`, `/history/transactions` (cursor pagination, max 50/page) | 20 / min |
| Reports | `POST /history/exports` + `GET /history/exports` (async CSV) | 1 / 30 s, 1 / min |
| Pies | `/pies...` — **deprecated** | — |

Rate limits are **per account** (not per key/IP); responses carry `x-ratelimit-*` headers.
Max **50 pending orders per ticker**.

## Order semantics
- **Sell = negative `quantity`**. Buy = positive.
- Market order placed while the market is closed is **queued for the next open**; `extendedHours=true`
  allows execution outside the regular session. Slippage warning is explicit.
- Stop and stop-limit trigger on the **Last Traded Price**.
- **Not idempotent** (beta): "Sending the same request multiple times may result in duplicate orders."
- Order status values: LOCAL, UNCONFIRMED, CONFIRMED, NEW, CANCELLING, CANCELLED, PARTIALLY_FILLED, FILLED,
  REJECTED, REPLACING, REPLACED. Fill types include TRADE, STOCK_SPLIT, SPIN_OFF, STOCK_DIVIDENDS, …

## Not provided by the API
- **No market data**: no quotes, candles or historical prices (positions only carry a `currentPrice`).
- No bracket/OCO orders: a stop-loss and a take-profit are two independent orders.
- No webhooks/streaming: status must be polled within the rate limits.
- No client order id field.

## Not documented (do NOT assume — verify on the demo account)
- Short selling (Invest/ISA are cash accounts; nothing in the spec mentions shorting).
- Minimum quantity / fractional precision per instrument; behaviour of a sell larger than the held quantity.
- FX conversion fees for non-USD accounts (see Trading 212's fee pages, not the API docs).
- Error codes inside 400 responses (spec only says "Failed validation").

## Consequences for QSTS (proposed, not implemented)
1. Market data stays with the data provider (Yahoo); the broker adapter is execution + account state only.
2. Long-only for this broker (existing rule: unknown short availability ⇒ not shortable).
3. Never auto-retry an order POST. On timeout/408/5xx: poll `GET /orders` and `/history/orders` and
   reconcile before any resend; persist our own client id ↔ broker id mapping.
4. Protective stops as separate `STOP` sell orders (GTC); cancel the sibling when the other leg fills —
   a window exists where both are live, so reconcile positions after every fill.
5. Client-side rate limiter per endpoint, honouring `x-ratelimit-reset`.
6. Ticker mapping via `/metadata/instruments` (e.g. `AAPL_US_EQ`); never construct tickers by string rules.
7. Paper first on `demo.trading212.com` with the PAPER key; LIVE remains gated by LiveSafetyGate.
   Settings need a key **and** secret per environment.
