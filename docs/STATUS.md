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
| 8 | Strategy Lab backend: experiment tracking, REPRODUCE EXPERIMENT, full validation pipeline, ablation | IMPLEMENTED (UI pending, phase 14) |
| 11 | Walk-forward (train/validate/test + embargo), OOS vault (logged, limited access), parameter robustness, Monte Carlo, cost sensitivity, PSR/DSR, Overfitting Risk Score, Strategy Score | IMPLEMENTED |
| 9 | AIProvider abstraction, AI research service (strategy proposals, news labels, result review) with schema validation, caching, daily budget, provenance | IMPLEMENTED |
| 9 | GeminiProvider | UNVERIFIED (API docs unreachable from build env; request shape unit-tested) |
| 10 | Evolutionary search (train/val min-fitness, complexity & divergence penalties, trial counting) | IMPLEMENTED |
| 12 | Strategy allocation (risk parity/inverse vol/min-var with shrinkage, health, cash), correlation clusters, effective bets, beta, sector exposure, confidence calibration, decay detection | IMPLEMENTED |
| 13/16 | BrokerAdapter, PaperBroker (same fill model as backtest), ExecutionService | IMPLEMENTED |
| 15 | NotificationService: log, email IMPLEMENTED; Telegram, Discord UNVERIFIED; WhatsApp PLACEHOLDER | PARTIAL |
| 18/19 | Manual approval queue, semi-automatic thresholds | IMPLEMENTED |
| 20/21 | LiveSafetyGate (config, mode, user confirmation, kill switch, approved strategy, paper report, data, errors), MICRO_LIVE | IMPLEMENTED (no live adapter exists) |
| 17 | Trading 212 adapter | NOT STARTED — blocked: official docs unreachable from environment |
| 13 | Market scanner (point-in-time), CLI `qsts scan` | IMPLEMENTED (scheduling via cron/Task Scheduler: not yet) |
| 14 | Desktop UI: FastAPI backend (127.0.0.1) + web frontend (dashboard, signals/approvals, charts with as-of replay, strategies, Strategy Lab, research/REPRODUCE, logs), `qsts desktop` native window via pywebview | IMPLEMENTED |
| 22 | Full validation on real data | NOT STARTED — needs real data access |

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
### Phase 8 / 11
- [x] Experiments store strategy version, dataset hashes, config, period, seed, code version; reproduce is bit-identical and detects tampered data
- [x] Folds ordered with embargo; params chosen on train, shortlist ranked on validation, single run on test
- [x] OOS hidden from research; every access logged; second access per version refused
- [x] Robustness neighbourhood scan; MC percentiles (5/25/50/75/95), ruin probability, insufficient-trade guard
- [x] DSR penalises many trials; ORS flags too-perfect/too-few-trades results
- [x] Pipeline returns REJECTED on random-walk data (no fake edge)
### Phases 9–21 (implemented parts)
- [x] AI output rejected when it invents features, adds unexpected fields (e.g. performance claims) or breaks numeric ranges; cached; budgeted
- [x] Evolution deterministic per seed; penalties only lower fitness; genomes always valid
- [x] Tech names cluster as one bet; allocation ignores past returns; 100% cash possible; calibration refuses to emit confidence without evidence
- [x] Decay: significant underperformance -> UNDER_REVIEW/DISABLE; too few trades -> no verdict
- [x] Paper broker: next-bar fills, idempotent orders, stop gaps, limit, partial fills, short/fractional rejection
- [x] Observation never executes; manual approval needs a human; kill switch blocks & cancels, keeps positions
- [x] Disconnect: block, notify, no position changes; reconnect requires reconciliation; divergence needs explicit user acceptance
- [x] Live gate blocks by default with explicit reasons; micro-live enforces MICRO_LIVE limits and capital cap
