# Status

Legend: **IMPLEMENTED** (tested) · **UNVERIFIED** (implemented, not tested against the real service) ·
**MOCKED** · **PLACEHOLDER** · **NOT STARTED**

| Phase | Component | Status |
|---|---|---|
| 0 | Architecture / specification | IMPLEMENTED (docs) |
| 1 | Config, env separation, modes, kill switch | IMPLEMENTED |
| 2 | Database schema (all spec tables + OOS access log, status history) | IMPLEMENTED |
| 3 | Market data: provider abstraction, CSV provider, quality engine, adjustments, repository, PIT store, universe membership | IMPLEMENTED |
| 3 | yfinance provider | UNVERIFIED (egress blocked in build env) |
| 4 | Indicators (trend/momentum/volatility/volume/structure/price action), versioned features, selection (IC, stability, redundancy, permutation, ablation) | IMPLEMENTED |
| 5 | Backtest engine, costs, partial fills, gaps, shorts, metrics, benchmarks, look-ahead checker | IMPLEMENTED |
| 6 | Strategy definitions (versioned DSL), regime engine, lifecycle | IMPLEMENTED |
| 7 | Risk engine & position sizing, MICRO_LIVE preset | IMPLEMENTED |
| 8–22 | Strategy Lab, AI, evolution, WF/OOS/MC, portfolio, paper, UI, notifications, brokers, live safety | NOT STARTED |

Implied volatility, market breadth, fundamentals/news/macro *providers*: NOT STARTED (schema + PIT queries exist).
Real historical S&P 500 membership source: NOT STARTED (needed before any multi-year universe backtest).

## Acceptance criteria
### Phase 1
- [x] Live disabled by default; live flag rejected outside `live`
- [x] Env-scoped broker keys; identical paper/live keys rejected; secrets hidden in repr
- [x] Mode progression cannot skip stages; real-money modes need explicit confirmation
- [x] Kill switch persists across processes, fail-safe on I/O error, release needs confirmation
### Phase 2
- [x] All required tables + indices; FK enforcement on SQLite
### Phase 3
- [x] Validates: duplicates (conflicting → reject), impossible OHLC, NaNs, non-session bars, future bars, missing sessions, stale data, extreme moves (unadjusted splits), zero-volume runs
- [x] Never fabricates missing data
- [x] Stores only validated data, idempotently; loads back exactly
- [x] Split & dividend adjustment; PIT fundamentals/news; PIT universe
- [x] MTF alignment cannot see unfinished higher-TF bars
### Phase 4
- [x] Every registered feature passes truncation causality test
- [x] Indicator reference values (Wilder RSI worksheet, SMA/WMA/ATR/Bollinger)
- [x] Feature selection drops noise & redundant duplicates on planted-signal data
### Phase 5
- [x] Next-open fills; stops, gaps, stop-before-target; R-multiple sizing; partial fills; shorts
- [x] Cash conservation (final equity = initial + Σ trade P&L)
- [x] Truncation test: past trades unchanged by future data
- [x] Causality checker detects a peeking feature
### Phase 6–7
- [x] Regime labels causal; lifecycle gates (evidence, human-only approval, history kept)
- [x] Risk: kill switch, loss limits, DD de-risking, sector & correlation caps, short availability, €100 micro-live sizing
