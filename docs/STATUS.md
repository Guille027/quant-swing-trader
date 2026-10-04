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
| 9/10 | Automatic research loop ("Investigación IA"): evolution seeded with past elites + Gemini proposals fed with research-only results → consistency score (worst of 3 sub-period Sharpes) → auto-validation of top candidates → leaderboard with global-trial DSR; one-time manual final OOS test; UI tab + `qsts autoresearch` | IMPLEMENTED (tested on synthetic data; smoke-run on real data, no strategy found yet) |
| 12 | Strategy allocation (risk parity/inverse vol/min-var with shrinkage, health, cash), correlation clusters, effective bets, beta, sector exposure, confidence calibration, decay detection | IMPLEMENTED |
| 13/16 | BrokerAdapter, PaperBroker (same fill model as backtest), ExecutionService | IMPLEMENTED |
| 16 | Paper trading ("Simulación", 2026-10-04): a CANDIDATE strategy (passed final test) is run FORWARD from today with the unchanged backtest engine (`close_at_end=False`), fictitious capital, orders for the next open (decided at the close, cash-limited), open positions, equity vs SPY, append-only evening journal with data-revision check; CANDIDATE→PAPER by a human only | IMPLEMENTED (tested on synthetic data; UI smoke-tested) |
| 15 | NotificationService: log, email IMPLEMENTED; Telegram, Discord UNVERIFIED; WhatsApp PLACEHOLDER | PARTIAL |
| 18/19 | Manual approval queue, semi-automatic thresholds | IMPLEMENTED |
| 20/21 | LiveSafetyGate (config, mode, user confirmation, kill switch, approved strategy, paper report, data, errors), MICRO_LIVE | IMPLEMENTED (no live adapter exists) |
| 17 | Trading 212 adapter | NOT STARTED — official API docs read 2026-10-03 (v0 beta), summary + implications in [TRADING212_API.md](TRADING212_API.md); awaiting go-ahead |
| 13 | Market scanner (point-in-time), CLI `qsts scan` | IMPLEMENTED (scheduling via cron/Task Scheduler: not yet) |
| 14 | Guided UI (2026-10-04): Inicio (4 steps + glossary), Datos (S&P 500 random sample / all / custom symbols / incremental update, background job with progress), one-click backtest of any ranked strategy vs SPY (research period only until its final test), expert tabs under "Avanzado" | IMPLEMENTED |
| 14 | Windows desktop shortcut (`Crear acceso directo.bat` / `qsts shortcut`): pythonw launcher without console, single server instance, Spanish close confirmation, browser fallback with keep-alive dialog, log in var/desktop.log, keep-awake during research/downloads | IMPLEMENTED (logic tested on Linux; not run on Windows from this environment) |
| 14 | Desktop UI: FastAPI backend (127.0.0.1) + web frontend (dashboard, signals/approvals, charts with as-of replay, strategies, Strategy Lab, research/REPRODUCE, logs), `qsts desktop` native window via pywebview | IMPLEMENTED |
| 22 | Full validation on real data | STARTED — baselines through the full pipeline on real data (below); no PIT S&P 500 membership yet |

Quarterly results (earnings): Yahoo earnings calendar via yfinance (past + upcoming, EPS estimate/reported/surprise),
stored in `earnings_events`, turned into point-in-time columns (info session = first close after the announcement; impact
session = first open after it; unknown time handled conservatively) and features `days_since_earnings`,
`earnings_surprise`, `days_to_earnings`; engine rules `earnings_blackout_days` / `exit_before_earnings` (research default
3 sessions + exit before results). IMPLEMENTED; Yahoo endpoint UNVERIFIED from the build environment (host blocked) —
coverage is shown in the Datos tab. Assumption: result dates are known 10 sessions ahead.
Implied volatility, market breadth, news/macro *providers*: NOT STARTED (schema + PIT queries exist). Historical news is
deliberately not scored with an LLM: the model knows what happened afterwards (hindsight leakage).
S&P 500 universe: CURRENT member list (datasets/s-and-p-500-companies on GitHub, Wikipedia fallback) with "Date added";
research ignores each stock's history before it joined (partial survivorship mitigation). Removed/delisted members are
still missing: a full point-in-time membership source remains NOT STARTED.

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

### Automatic research loop
- [x] Uses research-period data only; never opens the OOS vault (test asserts zero vault accesses)
- [x] Every evaluated strategy persisted and counted once (re-proposals of a known version are not re-counted)
- [x] Leaderboard DSR is deflated by the total number of trials (more trials -> lower DSR, tested)
- [x] Final test only for research-validated candidates, once per version; its results never reach the AI context
- [x] AI proposals: schema-validated, long-only enforced, malformed ones logged and not backtested

### Data manager / speed (2026-10-04)
- [x] Download jobs validate every symbol; failures reported per symbol, never filled
- [x] Rankings scoped by universe (symbols + window + scoring rules); trials still counted globally
- [x] Additive DB migration for new columns (existing databases keep working)
- [x] Opt-in feature cache in the research loop: bit-identical results (tested), ~45% faster per backtest

### Engine v2 (2026-10-04)
- [x] Equal-rank signals no longer filled in ticker alphabetical order (neutral, reproducible tie-break; test)
- [x] At the open: exits first, then entries in decision order; entry sizing no longer sees other symbols' same-day close
- [x] Rankings include the engine version: results from different engine versions are never mixed
- [x] Paper trading: evening orders = next-open fills; the past does not change when new days arrive (tests)

### Phases 9–21 (implemented parts)
- [x] AI output rejected when it invents features, adds unexpected fields (e.g. performance claims) or breaks numeric ranges; cached; budgeted
- [x] Evolution deterministic per seed; penalties only lower fitness; genomes always valid
- [x] Tech names cluster as one bet; allocation ignores past returns; 100% cash possible; calibration refuses to emit confidence without evidence
- [x] Decay: significant underperformance -> UNDER_REVIEW/DISABLE; too few trades -> no verdict
- [x] Paper broker: next-bar fills, idempotent orders, stop gaps, limit, partial fills, short/fractional rejection
- [x] Observation never executes; manual approval needs a human; kill switch blocks & cancels, keeps positions
- [x] Disconnect: block, notify, no position changes; reconnect requires reconciliation; divergence needs explicit user acceptance
- [x] Live gate blocks by default with explicit reasons; micro-live enforces MICRO_LIVE limits and capital cap
