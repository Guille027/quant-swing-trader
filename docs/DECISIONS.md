# Decisions log

1. **New repo instead of reusing StockIQ.** StockIQ is an education app that explicitly does not give
   signals; mixing a trading system into it would be wrong. Nothing reused.
2. **Python + SQLAlchemy + SQLite.** Best ecosystem for quant research; SQLite is zero-ops for a
   personal desktop app; the schema is portable to Postgres.
3. **Bar timestamps = bar open (UTC); availability = `available_at`.** Daily bars dated by session; their
   information becomes available at that session's close (NYSE calendar, early closes honoured).
4. **4H bars are session-aligned** (09:30–13:30, 13:30–16:00 ET), built only from completed 1H bars.
5. **Backward-adjusted prices** for signals/backtests; `raw_close` kept for absolute-price rules.
6. **Wilder smoothing seeded with SMA** (matches published reference values: RSI 70.46/66.25…).
7. **Conservative intrabar assumptions**: stop before target; gaps fill at the open.
8. **Unknown short availability ⇒ not shortable.**
9. **Uncalibrated confidence ⇒ no conviction sizing** (NORMAL at most; never displayed as a %).
10. **Network in the build environment blocks Yahoo/Stooq/Trading 212 docs**: the yfinance provider is
    implemented but unverified; Trading 212 integration is deferred until official docs can be read.
