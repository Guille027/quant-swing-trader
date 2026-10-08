# quant-swing-trader (QSTS) — strategy lab

Backtest the trading strategies you find (TradingView scripts, videos, books), see honestly whether they hold up, and
let the ones you choose trade by themselves on an **Alpaca paper account** (simulated money), with a Telegram message
for every trade. US stocks and ETFs, daily bars. **Live trading does not exist in this app.**

> A backtest is not evidence of future profits. Nothing in this repo claims a strategy is profitable.

## Quick start
```bash
python -m pip install -e ".[dev,desktop,yahoo,alpaca]"
python -m pytest
python -m qsts.cli desktop        # or: python -m qsts.cli serve  ->  http://127.0.0.1:8765
```

## Windows
- First time: double-click **`Instalar QSTS.bat`** (installs everything and creates the **QSTS** desktop icon).
- Updates: close QSTS and double-click **`Actualizar QSTS.bat`**; the version is shown at the bottom of the app.
- Logs: `var/desktop.log`. If QSTS cannot open, the window shows why.

## Using it
1. **Datos**: download prices (the S&P 500 or the symbols you want). The library offers to download what is missing.
2. **Biblioteca**: every row is a *bot* (a strategy on one stock): equity sparkline, 7/30/90-day and total return,
   win rate, profit factor. Search like `spy pf>1.5 win>55 dd<20 p90>5 sharpe>0.8 paper`. "Probar en otra acción"
   creates a bot on any stock.
3. **A bot's page**: metrics (backtest / paper), equity curve against buy & hold, performance metrics, trades, monthly
   P&L, Monte Carlo, **Auditoría** (nine honest checks), comparison and a TradingView-style report.
4. **Activar en paper**: choose the share of your Alpaca paper account. After each US close (22:00 Spain) the app
   downloads the prices, computes the signals and leaves the orders for the next open; stops/targets rest at Alpaca.
   Keep the app open (or open it before the next open). Every order and fill goes to Telegram.
5. **Ajustes**: Alpaca paper keys, Telegram bot, data copy between two computers (OneDrive or a file).

New strategies are added to `src/qsts/lab/strategies/` (one class per strategy, see `lab/strategy.py`); every
strategy is automatically tested for look-ahead.

## Layout
```
src/qsts/
  data/        Yahoo daily bars (stored raw), quality checks, split/dividend adjustment, NYSE calendar
  indicators/  causal indicators
  lab/         strategy contract + strategies, backtester, metrics, audit, library, paper trader
  broker/      Alpaca PAPER adapter (official SDK)
  notify/      Telegram
  app/         context, download jobs, desktop launcher, data copy between computers
  api/         local API used by the UI
  ui/static/   the app's screens
docs/          ARCHITECTURE, DECISIONS, STATUS
```
Screenshots in `docs/screenshots/` were made with SYNTHETIC random-walk prices: they show the screens, not results.
