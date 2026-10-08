# Status

v2 (October 2026): the app was restarted as a **strategy lab**. The earlier automatic strategy search, intraday lab
and simulation were removed (they remain in the git history).

### Backtests
- [x] Strategy contract: signals at the close, next-open execution, longs/shorts, stops/targets/trailing stops
- [x] Causality test for every registered strategy and every indicator
- [x] Single-stock backtester: exact fills on hand-made bars (signal→open, stop/target conservative, gaps, costs, reversals)
- [x] Metrics of the reference screens: net profit, win rate, profit factor, drawdown, EV, 7/30/90 days, Sharpe,
      Sortino, Calmar, ulcer, tail ratio, Kelly, VaR/CVaR, monthly P&L, weekday exposure, long/short report,
      buy & hold comparison, Monte Carlo
- [x] Audit ("Edge check") with 9 checks
- [x] S&P 500 portfolio bots: scanner over the index, ≤5 positions, ranked entries (author's rule or most liquid),
      each stock traded only from its date added; equivalence test with the single-stock backtester; per-stock
      breadth; audit with breadth, halves of the stocks and 100 random "monkey" portfolios; background computation
      cached on disk
- [x] Paper trading of intraday strategies: limit-on-open sent in Alpaca's OPG window, stop entries, protective
      stop after the fill, market-on-close exit from 15:40 New York (app must be open), missed close reported
- [x] Paper trading of portfolio bots (several stocks per bot, one Alpaca position per stock across bots)
- [x] Classic reference strategies: RSI(2) of Connors, golden cross 50/200, Turtles 20/10
- [x] 20 well-known published strategies, each with its source and a note on how it was interpreted:
      Connors RSI(2), Double 7's, Cumulative RSI, IBS, Turnaround Tuesday (intraday), gap-down fill (intraday),
      Crabel NR7 (intraday), TradingView's built-in MA cross / MACD / RSI / SuperTrend / Parabolic SAR / Bollinger,
      Ichimoku, Turtles 20/10 and 55/20, golden cross, Faber 10-month, Antonacci 12-month momentum, Minervini template
- [x] Intraday on daily bars: stop entries, limit-on-open entries, positions closed at the close (MOC); average
      holding time in the library (filter intradía / pocos días / semanas / meses)
- [ ] Strategies brought by the user (added one by one, each with its source)
- [ ] True intraday strategies on 5-minute bars (opening range, VWAP): need years of minute data (not in Yahoo)

### Paper trading (Alpaca paper account)
- [x] Adapter on the official SDK, paper only; requests validated by the SDK; keys from the UI into `.env`
- [x] Trader: data refresh after the close, signal → order queued for the next open, bracket/OTO for stops and
      targets, protective orders re-checked every evening (GTC, falling back to day), fills read back, ledger per bot,
      live curve continuing the backtest, reconciliation with Alpaca's positions, one bot per stock, ≤100% allocation,
      entries never sent late, shorts only where Alpaca allows them (tests with a fake Alpaca)
- [x] Telegram message for every order sent and every fill (stop / target / exit with the trade's result)
- [ ] Checked against the real Alpaca paper account (needs the user's keys; not reachable from the build environment)

### Running 24/7
- [x] `qsts server` (headless, 127.0.0.1, restarted by systemd), cloud-init script for an Oracle Cloud Always Free
      Ubuntu 24.04 machine with private access through Tailscale (`deploy/oracle/`, guide in `docs/SERVIDOR.md`)
- [x] Hand-over from the PC (copy + paper trading off there), upload of the copy from the browser, self-update
      button, per-computer "paper trading here" switch, automatic daily price download after each close
- [ ] Tried on a real Oracle machine (needs the user's accounts)

### App
- [x] New UI: library with sparklines, 7/30/90-day pills, advanced search; bot page with metrics (backtest / paper),
      equity curve, tabs (performance, trades, monthly P&L, Monte Carlo, audit, paper log), comparison, report
- [x] Paper trading page, data page, settings (Alpaca, Telegram, OneDrive copy), emergency STOP
- [x] Launcher recovers from a stuck previous instance and shows why a start fails
