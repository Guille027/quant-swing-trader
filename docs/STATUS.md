# Status

Legend: **IMPLEMENTED** (tested) · **UNVERIFIED** (implemented, not tested against the real service) ·
**MOCKED** · **PLACEHOLDER** · **NOT STARTED**

| Phase | Component | Status |
|---|---|---|
| 0 | Architecture / specification | IMPLEMENTED (docs) |
| 1 | Config, env separation, modes, kill switch | IMPLEMENTED |
| 2 | Database schema (all spec tables + OOS access log, status history) | IMPLEMENTED |
| 3 | Market data: provider abstraction, CSV provider, quality engine, adjustments, repository, PIT store, universe membership | IMPLEMENTED |
| 3 | yfinance provider | IMPLEMENTED + VERIFIED live 2026-10-03: SPY + 20 large caps 2010-01-04→2026-10-02 (4213 sessions each, 0 missing); raw reconstruction round-trips Yahoo Adj Close (max rel. err ≤ 9e-7); every raw EXTREME_MOVE is on a split/spin-off date. No independent second source reachable (Stooq reset, FRED blocked) |
| 4 | Indicators (trend/momentum/volatility/volume/structure/price action), versioned features, selection (IC, stability, redundancy, permutation, ablation) | IMPLEMENTED |
| 5 | Backtest engine, costs, partial fills, gaps, shorts, metrics, benchmarks, look-ahead checker | IMPLEMENTED |
| 6 | Strategy definitions (versioned DSL), regime engine, lifecycle | IMPLEMENTED |
| 7 | Risk engine & position sizing, MICRO_LIVE preset | IMPLEMENTED |
| 8 | Strategy Lab backend: experiment tracking, REPRODUCE EXPERIMENT, full validation pipeline, ablation | IMPLEMENTED (UI pending, phase 14) |
| 11 | Walk-forward (train/validate/test + embargo), OOS vault (logged, limited access), parameter robustness, Monte Carlo, cost sensitivity, PSR/DSR, Overfitting Risk Score, Strategy Score | IMPLEMENTED |
| 9 | AIProvider abstraction, AI research service (strategy proposals, news labels, result review) with schema validation, caching, daily budget, provenance | IMPLEMENTED |
| 9 | GeminiProvider | VERIFIED 2026-10-03: request/response fields match https://ai.google.dev/api/generate-content; live calls with the user key OK (`gemini-2.5-flash`), strategy proposals pass/fail schema validation as designed, cache hit avoids a second call |
| 10 | Evolutionary search (train/val min-fitness, complexity & divergence penalties, trial counting) | IMPLEMENTED |
| 12 | Strategy allocation (risk parity/inverse vol/min-var with shrinkage, health, cash), correlation clusters, effective bets, beta, sector exposure, confidence calibration, decay detection | IMPLEMENTED |
| 13/16 | BrokerAdapter, PaperBroker (same fill model as backtest), ExecutionService | IMPLEMENTED |
| 15 | NotificationService: log, email IMPLEMENTED; Telegram, Discord UNVERIFIED; WhatsApp PLACEHOLDER | PARTIAL |
| 18/19 | Manual approval queue, semi-automatic thresholds | IMPLEMENTED |
| 20/21 | LiveSafetyGate (config, mode, user confirmation, kill switch, approved strategy, paper report, data, errors), MICRO_LIVE | IMPLEMENTED (no live adapter exists) |
| 17 | Trading 212 adapter | NOT STARTED — official API docs read 2026-10-03 (v0 beta), summary + implications in [TRADING212_API.md](TRADING212_API.md); awaiting go-ahead |
| 13 | Market scanner (point-in-time), CLI `qsts scan` | IMPLEMENTED (scheduling via cron/Task Scheduler: not yet) |
| 14 | Desktop UI: FastAPI backend (127.0.0.1) + web frontend (dashboard, signals/approvals, charts with as-of replay, strategies, Strategy Lab, research/REPRODUCE, logs), `qsts desktop` native window via pywebview | IMPLEMENTED |
| 22 | Full validation on real data | STARTED — baselines through the full pipeline on real data (below); no PIT S&P 500 membership yet |

Implied volatility, market breadth, fundamentals/news/macro *providers*: NOT STARTED (schema + PIT queries exist).
Real historical S&P 500 membership source: NOT STARTED (needed before any multi-year universe backtest).

## Real-data runs (Yahoo, adjusted total-return prices; reproducible with `python scripts/run_baselines.py`)
Universe: AAPL BAC BRK-B CSCO CVX GE GOOGL HPQ IBM JNJ JPM KO MSFT ORCL PFE PG T WFC WMT XOM (US mega-caps at the
start of 2010, chosen from memory, NOT a verified point-in-time list; all still listed today → survivorship bias).
Research 2010-01-04→2022-12-30, OOS vault 2023-01-01→2026-10-02 (one logged access per version). Costs 5 bps spread
+ 5 bps slippage, no commission/FX. Params: SMA n / ROC n varied in walk-forward (20 folds).

| 2026-10-03 | CAGR IS / OOS | Sharpe IS / OOS | MaxDD IS / OOS | Decision |
|---|---|---|---|---|
| baseline_trend (close>SMA n=200) | 8.2% / 15.5% | 0.66 / 1.15 | -22% / -16% | REJECTED (fails beats_baselines) |
| baseline_momentum (ROC n=126 >0) | 11.0% / 16.5% | 0.82 / 1.25 | -24% / -15% | REJECTED (fails beats_baselines) |
| SPY buy & hold | 11.9% / 22.3% | 0.73 / 1.42 | -34% / -19% | reference |
| Equal-weight 20 buy & hold | 12.1% / 22.3% | 0.74 / 1.59 | -32% / -18% | reference |

Reading: both are ~90% invested long-only US large caps (mostly market beta); neither beats passive buy & hold
OOS. `beats_baselines` is tautological for the baselines themselves; the useful conclusion is that they are the
bar any new strategy must clear. Trade-bootstrap Monte Carlo understates drawdown (p5 -13% vs realised -22%)
because concurrent positions are not independent.

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
### Real data (phase 3/9/22)
- [x] Yahoo provider verified live; raw prices reconstructed (Yahoo OHLC/volume/dividends are split-adjusted)
- [x] Research, charts and scans use split/dividend-adjusted prices built from stored raw bars + actions
      (only actions with ex_date <= as-of time)
- [x] Gemini verified against official docs and a live call
- [x] Trading 212 official docs read and summarised (no code yet)
- [ ] Point-in-time S&P 500 membership source

### Phases 9–21 (implemented parts)
- [x] AI output rejected when it invents features, adds unexpected fields (e.g. performance claims) or breaks numeric ranges; cached; budgeted
- [x] Evolution deterministic per seed; penalties only lower fitness; genomes always valid
- [x] Tech names cluster as one bet; allocation ignores past returns; 100% cash possible; calibration refuses to emit confidence without evidence
- [x] Decay: significant underperformance -> UNDER_REVIEW/DISABLE; too few trades -> no verdict
- [x] Paper broker: next-bar fills, idempotent orders, stop gaps, limit, partial fills, short/fractional rejection
- [x] Observation never executes; manual approval needs a human; kill switch blocks & cancels, keeps positions
- [x] Disconnect: block, notify, no position changes; reconnect requires reconciliation; divergence needs explicit user acceptance
- [x] Live gate blocks by default with explicit reasons; micro-live enforces MICRO_LIVE limits and capital cap
