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
19. **Paper trading = the backtest engine run forward.** No second execution model: the same code decides at the
    close and fills at the next open with the same costs and sizing, so paper and research are comparable. Sessions
    start from the latest data only (no back-dating), journal each evening append-only and report vendor data
    revisions instead of rewriting history. Prices are total-return adjusted; dollar results are invariant to later
    backward adjustments (risk-based sizing), up to float noise.
20. **Engine v2.** Ties between equal-rank entry signals were broken alphabetically, which concentrated large-universe
    backtests in A–B tickers; now a per-day CRC32 key of (symbol, date) breaks ties (neutral and reproducible). Entry
    fills happen after all exits and in decision order, and sizing at the open uses previous closes only (v1 could
    see same-day closes of symbols processed earlier). Experiments recorded with v1 may no longer reproduce
    bit-identically (reported by REPRODUCE as differing metrics/code version); rankings are versioned.
21. **Earnings, not news.** For 1–20 day holds the results gap is the main event risk and post-earnings drift a documented
    effect, and the dates/surprises are available point-in-time. Historical news sentiment from an LLM would leak hindsight
    (the model was trained after the events), so news can only ever be used going forward, never to validate.
22. **Earnings timing is conservative.** Information is usable from the first CLOSE after the announcement; the gap is
    assumed at the first OPEN after it; with unknown time both the later information and the earlier gap are assumed.
    Research rankings include the earnings rule and a fingerprint of the earnings columns inside the research window.
    Paper trading reuses the exact engine rules of the strategy's validation experiment.
23. **Updates must take effect and previous research must carry over.** The launcher compares the running server's
    code version with the files on disk and replaces an older running copy (API shutdown, or ending the python process
    holding the port for versions that predate it); the UI is served with `Cache-Control: no-store` and shows its
    version. With the earnings rules off the ranking id is unchanged from before earnings existed; a new ranking
    re-scores the best strategies of earlier rankings first (counted as trials) instead of starting from zero.
24. **The final test must beat doing nothing.** The first final-test rule only required a positive OOS return and Sharpe,
    so a strategy that earned +9% while simply holding the same stocks earned about +80% in the same years was labelled
    "approved". A pass now requires (a) a positive OOS return, (b) an OOS Sharpe at least that of equal-weight buy & hold
    of the same stocks over the same OOS window and (c) an OOS Sharpe of at least half the research Sharpe (a larger
    decay means the research result was mostly selection luck). The passive and SPY OOS figures are computed only when
    judging and never reach the search or the AI. Earlier passes are re-judged from their stored OOS metrics (the vault
    is not reopened); those that fail are moved to REJECTED and any active paper session of them is stopped.
25. **Fiabilidad is measured against holding, not against zero.** The leaderboard DSR used the textbook null of a zero
    Sharpe. A long-only stock strategy invested most of the time gets a clearly positive Sharpe from the market alone,
    so a strategy no better than holding its own stocks could show ~80% "reliability". The null is now the Sharpe of
    equal-weight buy & hold of the same stocks over the research window (floored at 0 = cash) plus the expected best
    Sharpe of n unskilled tries: Fiabilidad = P(it truly beats holding the same stocks, after all the trials).
26. **A new ranking never looks like lost work.** A ranking only compares strategies scored on the same inputs (symbols,
    window, rules, earnings data), so downloading stocks or quarterly results starts a new one. The inputs of each
    ranking are now recorded (`research_universes`) and the UI says how many earlier strategies are kept, what changed,
    and that starting the search first re-scores the best 30 on the current data. Strategies that already had their
    one-time final test are not brought back. The options of the last search (e.g. avoid earnings) are remembered so
    the ranking shown after a restart uses the same rules.
27. **EUR accounts in a USD engine.** US stocks are quoted in dollars, so the simulation runs in USD with the starting
    euros converted at that day's EURUSD close, and values are shown in euros at each day's close (stored with the
    price downloads). Simplification, stated in the UI and code: the whole account is treated as dollars (at a broker
    with a EUR account, uninvested cash stays in euros) and currency conversion fees are not modelled, because they
    are not part of the cost model the strategy was validated with. Orders are given as euro amounts plus an
    approximate fractional share count; stops and targets stay in dollars, as the broker shows them.
28. **Telegram messages come from daily data.** Only daily bars are available, so stops and profit targets (hit during
    the day) are reported after the close, and the user is told to leave them at the broker as stop / limit orders.
    After each close (+45 min for the vendor) the app updates prices, recomputes the session and sends one message per
    day (plus a sell alert first when something must be sold or was closed); a day is recorded and never sent twice.
    If the app was closed, the latest close is sent when it opens: the orders are for the next open, so that is early
    enough. The bot token lives only in the git-ignored `.env`, is scrubbed from errors and never sent to the UI.
