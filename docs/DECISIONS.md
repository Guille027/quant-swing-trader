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
29. **Validation speed without changing results.** Validation re-ran every indicator for each of ~54 walk-forward
    windows because the data was cut at each window end first. Signals are now computed on the research history
    (cached) and the engine only trades inside the window; since every registered feature is causal (tested) this
    is bit-identical (tested against cutting first). The reference strategies are computed once per session,
    prepared price frames are cached while the stored data is unchanged (fingerprint of bars, corporate actions and
    earnings), validation reports its progress, and "Detener" also interrupts a validation (the candidate is simply
    validated again later).
30. **One idea must not take over the search.** Evolution seeded every cycle with the 6 best earlier strategies,
    which after a while were all variants of one indicator, so most trials only nudged its thresholds (and every
    near-duplicate trial still lowers everyone's Fiabilidad). Now: seeds keep at most 2 strategies per indicator
    idea (the set of indicators in the entry rules), no indicator may appear in more than half of a generation,
    20% of every generation is brand-new random strategies, and the AI is told which ideas are already crowded.
    Scoring is unchanged (rankings stay valid). The ranking can group variants of one idea (best one shown, with
    "+N variantes"); validated and final-tested strategies are always listed.
31. **Two computers share data through a copy, never a shared live database.** SQLite in a cloud-synced folder can
    be corrupted when the sync tool copies it mid-write, and two computers writing at once cannot be merged (trial
    counts, one-time OOS accesses, the paper journal). So the whole database is saved as one consistent snapshot
    (SQLite backup API, gzip, sha256, metadata written last) in a OneDrive folder when the app closes, and the other
    computer loads it at its next start after backing up its own data. Overwriting a copy that was not loaded, and
    researching or sending Telegram messages while a newer copy waits, are refused or warned about. Secrets stay in
    each computer's `.env`.
32. **A harder search instead of more final tests.** Many cycles produced research results far above buy & hold that
    failed the final test: the search was fitting noise. Three rules make it harder to fool, without touching the
    OOS vault: (1) the consistency score is the worst block Sharpe of the whole universe AND of two random halves of
    the stocks run separately (a rule that only fits some stocks fails); (2) the last 2 research years are a
    "pre-exam" the search never optimises on (fitness, walk-forward, robustness, costs, baselines, passive bar and
    the AI context all stop before it; feature thresholds come from the search window); finalists must pass it with
    the final-test rules before being offered the final test; (3) at most 2 entry conditions (evolution, AI,
    seeds, imports) and a complexity penalty of 0.05 instead of 0.03. This is a new ranking; the best simple-enough
    strategies of the previous one are re-scored first. Evaluation costs ~40% more (the halves reuse the signals).
33. **More ways to search, same guardrails.** The user asked to keep exploring: horizon 5/10/20 days and rules of up
    to 2 or 3 conditions are research options (each its own ranking, defaults keep the current one); "partir de
    cero" seeds only from what the current run finds (escaping one dominant idea) while results still join the same
    ranking. New causal features describe candles and calendar instead of indicators: close location in the range,
    body, wicks, NR-type range rank, range expansion, up/down streak, day of week, session of the month and sessions
    to month end (from the NYSE calendar, so no look-ahead). Every feature passes the automatic causality test.
34. **An intraday lab, with the same discipline and its own vault.** The user asked for opening-range breakouts on
    5-minute to 1-hour candles, long and short. Yahoo serves 5-minute bars for 60 sessions only and 1-hour bars for
    ~730 (checked live), so intraday bars are stored and accumulate. Rules (one trade per stock and day, flat at the
    close): range of the first 5-60 minutes, breakout by a closing bar (entry next open) or a stop order at the edge,
    stop at the other edge or the middle, optional target in R, entry window, and context filters (range vs daily
    ATR, opening volume vs its own past, opening gap, daily trend joined at the open via `align_higher_timeframe`).
    Fills are conservative (stop before target in the same bar, ambiguous bars skipped or stopped, gaps fill at the
    open; 10 bps per side; at most 10 trades a day, no leverage). Each dataset gets an out-of-sample boundary stored
    in the database at first use (last 25% of sessions); the search never loads those sessions, a new epoch starts
    only when the stored history has doubled. Same score (worst block of all stocks and two halves, complexity
    penalty), same pre-exam and final-test verdict, all trials counted with the daily ones. Below 120 search / 60
    vault sessions the lab is labelled exploratory and offers no validation or final test. Tests check exact fills
    on hand-made days, that earlier trades do not change when later data is removed, and that close-confirmed rules
    earn nothing on a random walk without costs. Shorts at Trading 212 need CFDs (stated in the UI, not modelled).
35. **The launcher must never wait silently on a dead port.** A start could wait 3 minutes and only point to the log
    when port 8765 was held by a previous QSTS that stopped answering (e.g. stuck while exiting). Now: a port that is
    held but silent is cleared if (and only if) a python process holds it, anything else is named to the user; an
    older version is closed completely (window and research), not only its server; the server thread is watched,
    so a failed start is reported at once with its reason and the last log lines in the window; local checks
    bypass system proxies; a start while the previous QSTS is still saving its data copy waits for it; the launcher
    process exits hard once everything is saved, so it can never linger holding the port or the database.
36. **More intraday patterns and a forward test, same rules.** Two families join the opening-range breakout: the
    opening gap (fade back to yesterday's close or follow it; entry at a later bar's open because the gap is only
    known after the open; stop in daily ATRs) and the VWAP (follow a cross, or bet on the return after a stretch;
    the VWAP uses bars up to the signal only). All share the conservative exit simulation and the lab's scoring;
    their trials are counted and every family passes the hand-made-day, no-look-ahead and random-walk tests.
    Existing ORB rules keep their identity. A frozen rule can be simulated day by day on sessions after it was
    started (a genuine forward test): each finished session is recorded once in an append-only journal (later data
    revisions never change it), flat every night so no overnight currency effect, and sent by Telegram. While the
    app is open, stored intraday bars are refreshed after each session, which is what lets the 60-day 5-minute
    history grow.
37. **Restart as a strategy lab.** Searching rules at random kept finding noise (every finalist failed out of sample).
    The user now brings concrete published strategies; each is coded as written (source noted), run on one stock per
    "bot", audited and, if chosen, paper traded on Alpaca. The old search, intraday lab, AI and simulation were removed
    from the code (still in git history); prices, downloads, Telegram, the launcher and the OneDrive copy were kept.
    Market and timeframe chosen for robustness: US stocks/ETFs on daily bars (long clean history; daily bars only need
    the app open once a day). Sizing: no leverage, a fixed share of the account per bot.
38. **Execution model = TradingView's default.** Signal at the close, market order at the next open; stops/targets
    are resting orders (gap fills at the open; a bar touching both counts as the stop). Live, the same thing: Alpaca
    queues a `day` order sent after the close for the next session (documented in the SDK); stops/targets are a
    bracket/OTO with the entry, re-checked every evening as GTC protective orders (falling back to day orders if GTC
    is refused, because the exit legs' time in force is not documented in the SDK). Entries are never sent once the
    market has opened (that would be a different price than the backtest); exits always are. Whole shares only.
39. **Audit instead of a single verdict.** A published backtest is a best case. Each bot gets nine checks (trades, buy
    & hold, costs, halves, years, neighbouring settings, other stocks, Monte Carlo, PSR) and the count of bots tried
    is shown; the real test is the paper curve (pink) from the activation date, on days nobody optimised on.
40. **Strategies are judged on the whole S&P 500, not on one chosen stock.** A strategy that only wins on the stock
    it was shown on is usually luck. Each strategy also runs as a portfolio bot: every session it scans today's S&P 500
    members and holds at most 5 positions (the user's limit), each with 1/5 of the equity at the previous close and
    never more than the equity not yet invested (no leverage). When more stocks signal than there are free places,
    the author's ranking rule is used if the source gives one; otherwise a fixed rule: most liquid first (20-session
    average traded value), simple, causal and what one would do in practice. Survivorship: the member list is today's;
    a stock is traded only from its "date added" (it was not in the index before, and stocks are often added after
    rising); removed companies are still missing and the screen says so (point-in-time member lists with delisted
    prices are not available from the data source). Extra audit checks: the strategy on each stock alone (breadth),
    on two alternating halves of the stocks, and against 100 random portfolios with the same entry frequency,
    holding periods and sizing ("monkeys", 95% required). A hidden hold-out period was not added: the app shows the
    whole curve, so the out-of-sample proof stays the paper trading from the activation date. With one stock and one
    place the portfolio engine reproduces the single-stock backtester exactly (tested).
41. **The 20 famous strategies, coded as published.** Chosen for being widely known and having fixed public rules
    (books, TradingView's built-in strategies, Quantified Strategies, papers); strategies that need data the app does
    not have (CANSLIM's earnings, IBD's RS rating, Darvas' volume reading) were left out or replaced as noted on their
    page. Common interpretation: authors who buy "at the close" are filled at the next open, because the signal is
    only known at the close. Ranking rules for the S&P 500 scanner only when the author gives one (Antonacci's
    relative momentum, Minervini's relative strength); otherwise the fixed "most liquid first".
42. **Intraday on daily bars.** Three order types make same-day strategies testable without minute data, each
    with an Alpaca equivalent documented in the SDK: stop entries (stop order, day), limit-on-open entries (limit +
    OPG; Alpaca only accepts OPG outside 9:28-19:00 New York, so they wait for that window) and exit at the close
    (market-on-close, CLS, accepted until 15:50 New York; sent from 15:40). Day-trade entries are simple orders and
    the protective stop is placed after the fill. This needs the app open during the US session; a missed close is
    closed at the next open and reported as a deviation. When a bar touches both the entry stop and the protective
    stop, the stop is assumed hit (the order inside a daily bar is unknown). True intraday strategies on 5-minute bars
    (opening range breakout, VWAP) need years of minute data, which Yahoo does not provide: not included.
43. **24/7 on a free server, private by design.** The user wanted the app running without the PC. Chosen: an
    Oracle Cloud Always Free machine set up entirely by a cloud-init script (no terminal needed), the app bound to
    127.0.0.1 and reached only through Tailscale (`tailscale serve --http=80`), so nothing is exposed to the
    internet and the app needs no login of its own. Exactly one computer may send orders: a per-computer switch
    (`QSTS_PAPER_HERE`); handing over from the PC saves the copy and turns trading off there in one step, and a
    computer with trading off refuses to activate or stop bots (it would cancel the server's orders). Keys are not
    in the copy; they are entered again on the server. Prices are downloaded automatically after each close
    (close + delay, up to 3 tries) on any computer running the app. Alternatives rejected: GitHub Actions (start
    times not guaranteed, state between runs), the phone (background apps are killed).
