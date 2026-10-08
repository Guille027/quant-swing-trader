# Working on this repo

- Install: `python -m pip install -e ".[dev]"`; test: `python -m pytest` (must stay green).
- The app is a strategy lab: the user brings published strategies (TradingView, videos, books); each one is coded
  as a class in `src/qsts/lab/strategies/` (one file per source, `@register`), backtested per stock ("bot"),
  audited, and can be paper traded automatically on the Alpaca PAPER account. Update `docs/STATUS.md` when a part
  is done and record design decisions in `docs/DECISIONS.md`.
- Integrity rules are non-negotiable: every strategy must be causal (signals at a close use only that bar and
  earlier ones; auto-tested for every registered strategy), all cross-timeframe/PIT joins go through
  `available_at`, no fabricated data/results. Code a strategy as its author published it; note any interpretation.
- Tests use SYNTHETIC data only; never present results on them as market evidence.
- Never enable live trading (paper only), never hardcode secrets (`.env` only), never invent broker/API behaviour
  (check the official SDK/docs; behaviour not documented there is handled defensively and said so).
- Keep progress messages short.
