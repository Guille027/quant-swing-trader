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

## Windows (cmd)
`qsts` may not be on PATH after `pip install`; use `python -m qsts.cli <command>` instead, e.g.
`python -m qsts.cli ingest --provider yahoo --symbols SPY,AAPL --start 2010-01-01` and `python -m qsts.cli desktop`.

## Use
```bash
qsts ingest --provider yahoo --symbols SPY,AAPL,MSFT --start 2010-01-01   # needs network access to Yahoo
qsts serve                     # UI at http://127.0.0.1:8765  (or: qsts desktop)
qsts scan                      # text dashboard
qsts research --strategy my.json --symbols AAPL,MSFT --oos-start 2022-01-01 --space '{"rsi_lo":[30,35,40]}'
qsts evolve --symbols AAPL,MSFT --train 2012-01-01:2017-12-31 --validate 2018-01-15:2021-12-31
python scripts/run_baselines.py --oos-start 2023-01-01   # baselines through the full pipeline (one OOS read per version)
qsts autoresearch --cycles 3   # automatic search for the most consistent strategy (also: UI tab "Investigación IA")
```
Screenshots in `docs/screenshots/` were taken with SYNTHETIC random-walk data (symbols `SYN_*`) — they show the UI, not market results.

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
See [docs/STATUS.md](docs/STATUS.md) for what is implemented, mocked or pending, and
[docs/TRADING212_API.md](docs/TRADING212_API.md) for what the Trading 212 API does and does not allow.
