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

## Desktop app (easiest)
`python -m qsts.cli desktop` → tab **Inicio** guides you: 1 · Datos (download S&P 500 stocks), 2 · Investigación IA
(automatic search), click a ranked strategy to see its backtest, Test final, then 3 · Simulación (paper trading:
what it would buy at the next open, with your capital in euros or dollars; nothing is sent to a broker). Optional:
a Telegram bot sends the next-open orders every evening (set up in 3 · Simulación → Avisos por Telegram).

## Windows: open it from the desktop
To update to the latest version: close QSTS and double-click **`Actualizar QSTS.bat`** (runs `git pull`).
Once: double-click **`Crear acceso directo.bat`** in the project folder (or run `python -m qsts.cli shortcut`).
It creates a **QSTS** icon on the desktop and in the Start menu; double-click it to open the app (no console).
`Abrir QSTS.bat` does the same without creating a shortcut. Logs go to `var/desktop.log`.
While a research run or a download is active, Windows is kept from sleeping (the screen may still turn off).

## A second computer (e.g. a laptop) with the same data
Install Python and Git there, `git clone` the repository, `git checkout claude/quirky-mayer-g6nhsj`, then
double-click **`Instalar QSTS.bat`** (installs the app with its native window and Yahoo downloads, and creates the
icon). Then on BOTH computers: Inicio → "Usar QSTS en
varios ordenadores" → the same OneDrive folder → Activar. Closing the app saves a compressed copy of the data there;
opening it on the other computer offers to load it (the local data is backed up to `var/backups` first). Use one
computer at a time: copies are not merged. If the cloud folder does not reach the other computer (e.g. different
OneDrive accounts), carry the copy as a file: "Guardar copia en Descargas" on one computer, download/copy
`qsts-datos.db.gz` into the other one's Downloads folder and press "Cargar la copia descargada". Secrets (`.env`: Gemini key, Telegram) are not copied: enter them in the
app on each computer (Investigación IA → Gemini key; Simulación → Telegram).

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
UI tab **Intradía**: downloads 5-minute or 1-hour bars (Yahoo: 60 days / ~730 days; stored bars accumulate and are
refreshed after each session while the app is open) for the most traded stocks you have and searches day-trading
rules (long/short, one trade per stock and day): opening-range breakout, opening gap (fade / follow) and VWAP
(cross / return), with the same discipline as the daily search (vault fixed at first use, pre-exam, one-time final
test). A chosen rule can be simulated day by day on new sessions (forward test, Telegram summary per session).
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
