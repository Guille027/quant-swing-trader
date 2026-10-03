# Architecture (Phase 0 specification)

## Layers
```
Desktop UI (phase 14)  ──►  Backend API (local)  ──►  Trading engine / Research engine / AI engine
                                                         │
                                                     Database (SQLite → Postgres-ready via SQLAlchemy)
```
Python 3.11 package `qsts`. Deterministic code does all numbers (prices, indicators, risk, backtests);
the AI layer (phase 9) only proposes hypotheses / interprets text and never produces numbers.

## Data flow
```
Provider (abstract) → raw bars → DataQualityEngine.validate_and_clean → ValidatedBars → store
ValidatedBars → features (causal, versioned) → strategy evaluation at bar close
→ risk engine → orders → (backtest | paper | live) execution adapter
```

## Integrity mechanisms (implemented)
| Risk | Mechanism |
|---|---|
| Look-ahead | Every bar has `available_at` (session close via NYSE calendar, incl. early closes). Multi-timeframe joins only via `align_higher_timeframe` (as-of on `available_at`). Orders fill at next bar open. Truncation-based causality tests for every registered feature and `check_strategy_causality` for strategies. |
| Swing-point leakage | Swing highs/lows indexed at their *confirmation* bar. |
| Ichimoku leakage | Chikou/forward shifts removed; documented causal usage. |
| Vol-percentile leakage | Rolling trailing percentile, never full-sample. |
| Unvalidated data | `ValidatedBars` cannot be constructed outside the quality engine; repository only stores `ValidatedBars`. |
| Fabricated data | Missing sessions are reported, never forward-filled. |
| Survivorship | `universe_membership` intervals + `universe_asof(date)`. |
| PIT fundamentals/news/macro | `available_at` column + `PointInTimeStore` as-of queries. |
| Labels | `forward_returns` is the only label generator and is documented as future information. |
| Reproducibility | Content-hash ids for datasets, features, strategy versions; seeds in configs. |
| Accidental live | Live requires `QSTS_ENV=live` + explicit flag; env-scoped keys; kill switch is a file flag independent of all other components. |

## Timing model (backtest = paper = live)
Decision at close of bar *t* → market order filled at open of *t+1* (+ half-spread + slippage + commission,
volume participation cap → partial fills). Stops/targets intrabar; gap through stop fills at the open; stop
assumed before target when both are touched in one bar.

## Planned components (see STATUS.md)
Walk-forward/OOS vault/Monte Carlo (11), robustness & overfitting score, Strategy Lab, AI research
(`AIProvider` → Gemini), evolutionary search, portfolio/correlation engine, paper broker, `BrokerAdapter`
(Trading 212 only after verifying official docs), notifications, desktop UI.
