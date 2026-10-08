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
- [x] Classic reference strategies: RSI(2) of Connors, golden cross 50/200, Turtles 20/10
- [ ] Strategies brought by the user (added one by one, each with its source)

### Paper trading (Alpaca paper account)
- [x] Adapter on the official SDK, paper only; requests validated by the SDK; keys from the UI into `.env`
- [x] Trader: data refresh after the close, signal → order queued for the next open, bracket/OTO for stops and
      targets, protective orders re-checked every evening (GTC, falling back to day), fills read back, ledger per bot,
      live curve continuing the backtest, reconciliation with Alpaca's positions, one bot per stock, ≤100% allocation,
      entries never sent late, shorts only where Alpaca allows them (tests with a fake Alpaca)
- [x] Telegram message for every order sent and every fill (stop / target / exit with the trade's result)
- [ ] Checked against the real Alpaca paper account (needs the user's keys; not reachable from the build environment)

### App
- [x] New UI: library with sparklines, 7/30/90-day pills, advanced search; bot page with metrics (backtest / paper),
      equity curve, tabs (performance, trades, monthly P&L, Monte Carlo, audit, paper log), comparison, report
- [x] Paper trading page, data page, settings (Alpaca, Telegram, OneDrive copy), emergency STOP
- [x] Launcher recovers from a stuck previous instance and shows why a start fails
