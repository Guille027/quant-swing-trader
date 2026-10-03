# Working on this repo

- Install: `python -m pip install -e ".[dev]"`; test: `python -m pytest` (must stay green).
- Follow the master spec's phase order; update `docs/STATUS.md` checklists when a phase completes.
- Integrity rules are non-negotiable: every new feature must be causal (it is auto-tested via the
  registry), all cross-timeframe/PIT joins go through `available_at`, no fabricated data/results.
- Tests use SYNTHETIC random-walk data only; never present results on them as market evidence.
- Never enable live trading, never hardcode secrets, never invent broker/API behaviour.
- Keep progress messages short.
