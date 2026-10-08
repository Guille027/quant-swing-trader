# Architecture (v2: strategy lab)

```
Desktop window (pywebview) / browser  ──►  local API (FastAPI, 127.0.0.1)  ──►  lab: backtests, audit, paper trader
                                                                                    │            │
                                                                     SQLite (prices, bots, orders)   Alpaca PAPER (alpaca-py)
```
Python 3.11 package `qsts`:

| Module | What it does |
|---|---|
| `data/` | Yahoo daily bars stored RAW, quality checks (`ValidatedBars`), split/dividend adjustment, NYSE calendar, `available_at` |
| `indicators/` | causal indicators (moving averages, RSI, ATR, MACD, Supertrend...), tested for look-ahead |
| `lab/strategy.py` | how a strategy is written (signals at a close, executed at the next open) + registry |
| `lab/strategies/` | the strategies, one file per source |
| `lab/backtest.py` | single-stock backtest with TradingView's default execution model, conservative stop/target fills |
| `lab/portfolio.py` | portfolio backtest: a strategy as a scanner over the S&P 500, at most 5 positions, ranked entries; per-stock breadth; random "monkey" portfolios |
| `lab/metrics.py` | every number on a bot's page (TradingView report, quantstats-style ratios, Monte Carlo) |
| `lab/audit.py` | "Edge check": trades, costs, halves, years, neighbours, other stocks, Monte Carlo, PSR (+ breadth, halves of the stocks and monkeys for portfolio bots) |
| `lab/service.py` | the library of bots (strategy × stock, or strategy × S&P 500), results cached; portfolio bots computed by a background worker and cached on disk |
| `lab/trader.py` | automatic paper trading: after each close, signals → orders queued for the next open; fills read back |
| `broker/alpaca_paper.py` | Alpaca PAPER account through the official SDK (`paper=True` only) |
| `notify/telegram.py` | Telegram Bot API client |
| `app/` | context, data download jobs, launcher (desktop window), data copy between computers (OneDrive) |
| `api/server.py` | endpoints used by the UI |
| `ui/static/` | the single-page UI |

## Data flow
```
Yahoo → raw bars → validate_and_clean → store (raw) → research_frame (adjusted) → strategy.signals (causal)
→ backtest (next-open fills) → metrics / audit → UI
                         └→ paper trader: last close's signal → Alpaca order (day, queued for the open) → fills → Telegram
```

## Integrity mechanisms
| Risk | Mechanism |
|---|---|
| Look-ahead in a strategy | signals decided at the close, filled at the next open; `test_every_strategy_is_causal` truncates history for every registered strategy |
| Look-ahead in indicators | `test_every_indicator_is_causal` |
| Flattering fills | stop before target when a daily bar touches both; gaps through a stop fill at the open; slippage on market/stop fills |
| Survivorship (today's S&P 500 list) | a stock is only traded from its "date added" to the index; the remaining bias (removed companies missing) is shown on the screen |
| Lucky stock | portfolio bots run on the whole index; the strategy is also run on every stock alone and on two halves of the stocks |
| Edge vs. exposure | 100 random portfolios with the same entry frequency, holding time and sizing ("monkeys") must be beaten 95% of the time |
| Leverage | position size capped at 100% of the bot's equity; total paper allocation capped at 100% of the account |
| Survivorship / one lucky stock | audit runs the same rules on liquid stocks and ETFs |
| Overfitting | audit: halves, years, neighbouring settings, double costs, Monte Carlo, PSR vs buy & hold; number of bots tried is shown |
| Live money | the broker adapter is created with `paper=True` only; no live adapter exists |
| Secrets | `.env` only, entered from the UI, never sent back to the screen |
