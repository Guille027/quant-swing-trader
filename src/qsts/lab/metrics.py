"""Every number on a bot's page, computed from its equity curve (one value per trading day) and its trades.

Ratios follow the usual definitions (the ones used by the `quantstats` library and TradingView's strategy report);
each is documented next to its formula. Nothing is estimated or filled in: a metric that cannot be computed (too
few trades, no losses...) is None.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

DAYS = 252
WEEKDAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def longest_drawdown_days(equity: pd.Series) -> int:
    """Longest time (calendar days) spent below a previous high."""
    if len(equity) < 2:
        return 0
    under = (equity < equity.cummax()).to_numpy()
    best, start = 0, None
    t = equity.index
    for i, u in enumerate(under):
        if u and start is None:
            start = i - 1
        if (not u or i == len(under) - 1) and start is not None:
            end = i if u else i
            best = max(best, (t[end] - t[start]).days)
            start = None
    return int(best)


def window_return(equity: pd.Series, days: int) -> float | None:
    """Change of the equity over the last `days` calendar days."""
    if len(equity) < 2:
        return None
    past = equity[equity.index <= equity.index[-1] - pd.Timedelta(days=days)]
    if not len(past):
        return None
    return _f(equity.iloc[-1] / past.iloc[-1] - 1)


def trade_stats(tr: pd.DataFrame) -> dict:
    """TradingView-style trade statistics for a set of closed trades."""
    n = len(tr)
    if n == 0:
        return {"n_trades": 0, "net_profit": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": None,
                "win_rate": None, "avg_trade": None, "avg_trade_pct": None, "avg_win": None, "avg_loss": None,
                "payoff": None, "largest_win": None, "largest_loss": None, "avg_bars": None,
                "n_wins": 0, "n_losses": 0, "max_consec_wins": 0, "max_consec_losses": 0}
    pnl = tr["pnl"].to_numpy(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gp, gl = float(wins.sum()), float(-losses.sum())
    streak_w = streak_l = best_w = best_l = 0
    for p in pnl:
        if p > 0:
            streak_w, streak_l = streak_w + 1, 0
        else:
            streak_w, streak_l = 0, streak_l + 1
        best_w, best_l = max(best_w, streak_w), max(best_l, streak_l)
    avg_w = float(wins.mean()) if len(wins) else None
    avg_l = float(-losses.mean()) if len(losses) else None
    return {"n_trades": n, "net_profit": float(pnl.sum()), "gross_profit": gp, "gross_loss": gl,
            "profit_factor": _f(gp / gl) if gl > 0 else None, "win_rate": len(wins) / n,
            "avg_trade": float(pnl.mean()), "avg_trade_pct": _f(tr["pnl_pct"].mean()),
            "avg_win": avg_w, "avg_loss": avg_l, "payoff": _f(avg_w / avg_l) if avg_w and avg_l else None,
            "largest_win": _f(wins.max()) if len(wins) else None, "largest_loss": _f(-losses.max()) if len(losses) else None,
            "avg_bars": _f(tr["bars"].mean()), "n_wins": int(len(wins)), "n_losses": int(len(losses)),
            "max_consec_wins": best_w, "max_consec_losses": best_l}


def summary(equity: pd.Series, trades: pd.DataFrame, capital: float, position: pd.Series | None = None) -> dict:
    """Headline metrics (library row and the top of a bot's page)."""
    ts = trade_stats(trades)
    r = equity.pct_change().dropna()
    dd = drawdown(equity)
    net = equity.iloc[-1] / capital - 1 if len(equity) else 0.0
    sd = r.std(ddof=1)
    return {"net_profit_pct": _f(net), "win_rate": ts["win_rate"], "profit_factor": ts["profit_factor"],
            "max_drawdown": _f(dd.min()) if len(dd) else None, "n_trades": ts["n_trades"],
            "ev_pct": ts["avg_trade_pct"],          # expected value of a trade, % of the position
            "ev_payoff": ts["avg_trade"],           # expected value of a trade, in money
            "sharpe": _f(r.mean() / sd * np.sqrt(DAYS)) if len(r) > 2 and sd > 0 else None,
            "cagr": _f((equity.iloc[-1] / capital) ** (365.25 / max((equity.index[-1] - equity.index[0]).days, 1)) - 1)
            if len(equity) > 1 and equity.iloc[-1] > 0 else None,
            "exposure": _f((position != 0).mean()) if position is not None and len(position) else None,
            "d7": window_return(equity, 7), "d30": window_return(equity, 30), "d90": window_return(equity, 90),
            "first": str(equity.index[0].date()) if len(equity) else None,
            "last": str(equity.index[-1].date()) if len(equity) else None}


def key_metrics(equity: pd.Series, trades: pd.DataFrame) -> dict:
    """The 'Key performance metrics' list (daily returns of the equity curve)."""
    r = equity.pct_change().dropna()
    out: dict = {}
    if len(r) < 3 or r.std(ddof=1) == 0:
        return out
    dd = drawdown(equity)
    mean, sd = r.mean(), r.std(ddof=1)
    downside = np.sqrt((np.minimum(r, 0) ** 2).mean())
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    ulcer = float(np.sqrt((dd ** 2).mean()))                       # Ulcer index: RMS of the drawdowns
    q95, q05 = r.quantile(0.95), r.quantile(0.05)
    var = norm.ppf(0.05, mean, sd)                                 # daily value at risk (95%, normal)
    cvar = r[r <= var].mean() if (r <= var).any() else var        # expected shortfall beyond the VaR
    ts = trade_stats(trades)
    tail = abs(q95 / q05) if q05 != 0 else None                    # right tail vs left tail
    gain_pain = r.sum() / abs(r[r < 0].sum()) if (r < 0).any() else None
    pitfall = -cvar / sd if sd > 0 else None
    out.update({
        "sharpe": mean / sd * np.sqrt(DAYS),
        "sortino": mean / downside * np.sqrt(DAYS) if downside > 0 else None,
        "calmar": cagr / abs(dd.min()) if dd.min() < 0 else None,
        "longest_dd_days": longest_drawdown_days(equity),
        "volatility": sd * np.sqrt(DAYS),
        "skew": skew(r), "kurtosis": kurtosis(r, fisher=True),
        "expected_daily": mean, "expected_monthly": (1 + mean) ** 21 - 1, "expected_yearly": (1 + mean) ** DAYS - 1,
        # Kelly fraction from the trades: W - (1 - W) / payoff
        "kelly": ts["win_rate"] - (1 - ts["win_rate"]) / ts["payoff"] if ts["win_rate"] is not None and ts["payoff"] else None,
        "var_daily": var, "cvar_daily": cvar,
        "max_consec_wins": ts["max_consec_wins"], "max_consec_losses": ts["max_consec_losses"],
        "n_wins": ts["n_wins"], "n_losses": ts["n_losses"],
        "gain_pain": gain_pain, "payoff": ts["payoff"],
        "common_sense": ts["profit_factor"] * tail if ts["profit_factor"] and tail else None,
        "tail_ratio": tail,
        "outlier_win": r.quantile(0.99) / r[r >= 0].mean() if (r >= 0).any() and r[r >= 0].mean() > 0 else None,
        "outlier_loss": r.quantile(0.01) / r[r < 0].mean() if (r < 0).any() else None,
        "recovery_factor": (equity.iloc[-1] / equity.iloc[0] - 1) / abs(dd.min()) if dd.min() < 0 else None,
        "ulcer": ulcer,
        "serenity": r.sum() / (ulcer * pitfall) if ulcer > 0 and pitfall else None,
        "first_trade": str(trades["entry_time"].iloc[0].date()) if len(trades) else None,
        "last_trade": str(trades["exit_time"].iloc[-1].date()) if len(trades) else None,
    })
    return {k: (_f(v) if not isinstance(v, (int, str)) or isinstance(v, bool) else v) for k, v in out.items()}


def report_by_side(trades: pd.DataFrame, capital: float) -> dict:
    """TradingView's 'Performance' table for all trades, longs and shorts."""
    out = {}
    for name, sub in (("all", trades), ("long", trades[trades["side"] == "largo"]),
                      ("short", trades[trades["side"] == "corto"])):
        st = trade_stats(sub)
        st["net_profit_pct"] = st["net_profit"] / capital
        st["gross_profit_pct"] = st["gross_profit"] / capital
        st["gross_loss_pct"] = st["gross_loss"] / capital
        out[name] = {k: _f(v) if isinstance(v, float) else v for k, v in st.items()}
    return out


def monthly_returns(equity: pd.Series) -> list[dict]:
    """Month-by-month returns (year rows), plus the year's total."""
    if len(equity) < 2:
        return []
    m = equity.resample("ME").last()
    first = equity.iloc[0]
    rets = m.pct_change()
    rets.iloc[0] = m.iloc[0] / first - 1
    rows = []
    for y, grp in rets.groupby(rets.index.year):
        months = {int(t.month): _f(v) for t, v in grp.items()}
        start = equity[equity.index.year < y]
        base = start.iloc[-1] if len(start) else first
        year_end = equity[equity.index.year == y].iloc[-1]
        rows.append({"year": int(y), "months": months, "total": _f(year_end / base - 1)})
    return rows


def weekday_exposure(position: pd.Series) -> list[dict]:
    """Average open positions per weekday (share of those weekdays with a position open at the close)."""
    if position is None or not len(position):
        return []
    on = (position != 0).astype(float)
    g = on.groupby(on.index.dayofweek).mean()
    return [{"day": WEEKDAYS[d], "value": _f(g.get(d, 0.0))} for d in range(5)]


def pnl_range(equity: pd.Series, capital: float) -> dict:
    """Current, best and worst cumulative P&L reached (the benchmarking bars)."""
    if not len(equity):
        return {"current": None, "max": None, "min": None}
    p = equity / capital - 1
    return {"current": _f(p.iloc[-1]), "max": _f(p.max()), "min": _f(p.min())}


def sparkline(equity: pd.Series, points: int = 60) -> list[float]:
    if not len(equity):
        return []
    idx = np.linspace(0, len(equity) - 1, min(points, len(equity))).round().astype(int)
    return [round(float(v), 2) for v in equity.iloc[idx]]


def monte_carlo(trades: pd.DataFrame, n_sims: int = 1000, seed: int = 0) -> dict | None:
    """Re-draws the trades (with replacement, same number) many times: how different could the result have been
    with the same edge but another order / luck? Returns percentiles of the final return and of the max drawdown,
    the probability of ending with a loss and percentile bands of the equity path (per trade)."""
    rets = trades["return"].to_numpy(dtype=float) if len(trades) else np.array([])
    if len(rets) < 10:
        return None
    rng = np.random.default_rng(seed)
    draws = rng.choice(rets, size=(n_sims, len(rets)), replace=True)
    paths = np.cumprod(1 + draws, axis=1)
    finals = paths[:, -1] - 1
    peak = np.maximum.accumulate(np.concatenate([np.ones((n_sims, 1)), paths], axis=1), axis=1)[:, 1:]
    mdd = (paths / peak - 1).min(axis=1)
    pct = [5, 25, 50, 75, 95]
    step = max(1, len(rets) // 80)
    cols = list(range(0, len(rets), step)) + ([len(rets) - 1] if (len(rets) - 1) % step else [])
    bands = {str(p): [round(float(v), 4) for v in np.percentile(paths[:, cols], p, axis=0) - 1] for p in (5, 50, 95)}
    return {"n_sims": n_sims, "n_trades": int(len(rets)),
            "final": {str(p): _f(np.percentile(finals, p)) for p in pct},
            "max_drawdown": {str(p): _f(np.percentile(mdd, 100 - p)) for p in pct},
            "p_loss": _f((finals < 0).mean()), "bands": bands, "band_trades": [c + 1 for c in cols]}


def yearly(equity: pd.Series, bench: pd.Series | None = None) -> list[dict]:
    out = []
    for y in sorted(set(equity.index.year)):
        e = equity[equity.index.year == y]
        prev = equity[equity.index.year < y]
        base = prev.iloc[-1] if len(prev) else e.iloc[0]
        row = {"year": int(y), "strategy": _f(e.iloc[-1] / base - 1)}
        if bench is not None and len(bench):
            b = bench[bench.index.year == y]
            bp = bench[bench.index.year < y]
            if len(b):
                row["benchmark"] = _f(b.iloc[-1] / (bp.iloc[-1] if len(bp) else b.iloc[0]) - 1)
        out.append(row)
    return out


def psr(returns: pd.Series, benchmark_sr: float = 0.0) -> float | None:
    """Probabilistic Sharpe ratio: chance that the true (daily) Sharpe beats `benchmark_sr`, given the length,
    skewness and fat tails of the sample (Bailey & Lopez de Prado)."""
    r = returns.dropna()
    if len(r) < 30 or r.std(ddof=1) == 0:
        return None
    sr = r.mean() / r.std(ddof=1)
    g3, g4 = skew(r), kurtosis(r, fisher=False)
    den = np.sqrt(max(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2, 1e-12))
    return _f(norm.cdf((sr - benchmark_sr) * np.sqrt(len(r) - 1) / den))
