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
10. **Network in the build environment blocked Yahoo/Stooq/Trading 212 docs** (until 2026-10-03). With network open:
    Yahoo and the Trading 212 / Gemini docs are reachable; Stooq resets connections and FRED is blocked by policy.
11. **Store RAW prices, adjust on read.** Yahoo's `auto_adjust=False` OHLC, volume and dividends are already
    split-adjusted, so the provider multiplies back by later split ratios to recover raw values. Adjustment
    (split + dividend, CRSP-style backward factors) happens in `research_frame` / charts / scans, using only
    corporate actions with ex_date <= the as-of time. Verified: equals Yahoo Adj Close to float precision.
12. **Spin-offs** are reported by Yahoo as fractional "splits" (T 2022 1.324, GE 2023 1.281, IBM 2021 1.046…).
    Treating them as splits keeps prices continuous; the volume of bars before a spin-off is scaled by the same
    factor, which is wrong for volume (share count did not change) — acceptable for now, documented.
13. **Integer parameters**: params are stored as floats; window-length params (detected from feature defaults /
    stop & holding fields) are varied in integer steps by the robustness scan.
14. **`.gitignore` anchors `/data/`** — the unanchored `data/` silently excluded `src/qsts/data/` from git.
15. **Automatic research = search + strict accounting, not "learning" in the ML sense.** Fitness is the WORST
    annualised Sharpe of 3 consecutive research sub-periods minus 0.03 per complexity point (consistency over
    average). Each cycle the evolution starts from the best evolved strategies so far and the AI sees a summary
    of research-period results. Because a long search on fixed history always finds lucky rules, every trial is
    stored and the leaderboard DSR uses the global trial count and the cross-trial Sharpe variance (conservative:
    similar strategies are counted as independent). The OOS vault is only opened by an explicit, once-per-version
    final test of a research-validated candidate; its result is never fed back to the search or the AI.
16. **Bigger universes from the current S&P 500 list, sampled at random.** Hand-picking today's well-known names
    bakes hindsight into a backtest; the UI offers a seeded random sample (100/250) or all ~500. Each stock's
    history before its "Date added" is ignored by research (setting `QSTS_PIT_MEMBERSHIP`). Companies that left the
    index are still absent, so long-only results remain optimistic; this is stated in the UI.
17. **Rankings are per universe.** A strategy's score depends on the symbols and window it was scored on, so the
    leaderboard only compares rows with the same `universe_id`; the Deflated Sharpe still counts every trial ever run.
18. **Speed without changing numbers.** Research reuses indicator results through a content-keyed cache (exact frame
    hash) instead of re-implementing indicators faster, because faster maths would change last-bit results and break
    bit-identical reproduction of stored experiments.
