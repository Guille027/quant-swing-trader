# quant-swing-trader (QSTS)

Quantitative research platform + (eventually) swing-trading system for US equities (S&P 500).
**Research platform first, trading bot second.** Live trading is DISABLED by default.

> NO EVIDENCE → NO TRADE. Nothing in this repo claims any strategy is profitable.

## Quick start
```bash
python -m pip install -e ".[dev]"          # add ,yahoo for the yfinance provider
cp .env.example .env
python -m pytest
```

## Layout
```
src/qsts/
  config.py            environment-scoped settings (dev/paper/live key isolation)
  core/                modes, kill switch, hashing, logging
  db/                  SQLAlchemy schema (all tables), engine/session
  data/                bars & timeframes, data-quality engine, providers, adjustments, repository
  indicators/          causal technical indicators, market structure, price action
  features/            versioned feature registry, feature selection
  strategy/            declarative strategy definitions, regime engine, lifecycle
  backtest/            event-driven engine, metrics, benchmarks, look-ahead checker
  risk/                risk engine & position sizing (incl. MICRO_LIVE preset)
docs/                  ARCHITECTURE, STATUS (phase checklists), DECISIONS
```
See [docs/STATUS.md](docs/STATUS.md) for what is implemented, mocked or pending.
