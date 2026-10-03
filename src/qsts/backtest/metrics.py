"""Performance metrics. Every metric is computed from the equity curve / trade list actually produced
by the engine; nothing here estimates or fills in missing results."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _streaks(wins: np.ndarray) -> tuple[int, int]:
    best_w = best_l = cur = 0
    last = None
    for w in wins:
        cur = cur + 1 if w == last else 1
        last = w
        if w:
            best_w = max(best_w, cur)
        else:
            best_l = max(best_l, cur)
    return best_w, best_l


def drawdown_series(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def _drawdown_stats(equity: pd.Series) -> dict:
    dd = drawdown_series(equity)
    in_dd = dd < 0
    episodes, recov = [], []
    start = None
    for i, flag in enumerate(in_dd.to_numpy()):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            episodes.append(dd.iloc[start:i].min())
            recov.append(i - start)
            start = None
    if start is not None:
        episodes.append(dd.iloc[start:].min())
    return {
        "max_drawdown": float(dd.min()) if len(dd) else 0.0,
        "avg_drawdown": float(np.mean(episodes)) if episodes else 0.0,
        "max_recovery_bars": int(max(recov)) if recov else 0,
        "unrecovered_at_end": bool(start is not None),
    }


def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame, bars_per_year: int = 252,
                    risk_free_annual: float = 0.0) -> dict:
    eq = equity["equity"].astype(float)
    n = len(eq)
    out: dict = {"n_bars": n}
    if n < 2:
        return out | {"insufficient_data": True}
    rets = eq.pct_change().dropna()
    years = n / bars_per_year
    total = eq.iloc[-1] / eq.iloc[0] - 1
    rf_bar = (1 + risk_free_annual) ** (1 / bars_per_year) - 1
    ex = rets - rf_bar
    vol = rets.std(ddof=1) * np.sqrt(bars_per_year)
    downside = np.sqrt((np.minimum(ex, 0) ** 2).mean()) * np.sqrt(bars_per_year)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    dds = _drawdown_stats(eq)
    out |= {
        "total_return": float(total),
        "cagr": float(cagr),
        "annualized_return": float(rets.mean() * bars_per_year),
        "volatility": float(vol),
        "sharpe": float(ex.mean() / rets.std(ddof=1) * np.sqrt(bars_per_year)) if rets.std(ddof=1) > 0 else np.nan,
        "sortino": float(ex.mean() * bars_per_year / downside) if downside > 0 else np.nan,
        **dds,
        "calmar": float(cagr / abs(dds["max_drawdown"])) if dds["max_drawdown"] < 0 else np.nan,
        "exposure": float((equity["gross_exposure"] > 0).mean()) if "gross_exposure" in equity else np.nan,
        "avg_gross_exposure": float(equity["gross_exposure"].mean()) if "gross_exposure" in equity else np.nan,
    }
    out |= trade_metrics(trades, eq.iloc[0], years)
    return out


def trade_metrics(trades: pd.DataFrame, initial_equity: float, years: float) -> dict:
    nt = len(trades)
    if nt == 0:
        return {"n_trades": 0}
    pnl = trades["pnl"].to_numpy()
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    w_streak, l_streak = _streaks(pnl > 0)
    notional = (trades["qty"] * trades["entry_price"]).sum() + (trades["qty"] * trades["exit_price"]).sum()
    return {
        "n_trades": int(nt),
        "trades_per_year": float(nt / years) if years > 0 else np.nan,
        "win_rate": float(len(wins) / nt),
        "loss_rate": float(len(losses) / nt),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "expectancy": float(pnl.mean()),
        "expectancy_r": float(trades["r_multiple"].mean()),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else np.inf,
        "payoff_ratio": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else np.nan,
        "avg_trade_bars": float(trades["bars_held"].mean()),
        "longest_win_streak": int(w_streak),
        "longest_loss_streak": int(l_streak),
        "turnover_annual": float(notional / initial_equity / years) if years > 0 else np.nan,
        "total_costs": float(trades["costs"].sum()),
    }


def periodic_returns(equity: pd.Series, freq: str) -> pd.Series:
    """freq: 'YE' yearly or 'ME' monthly compounded returns."""
    e = equity.copy()
    e.index = pd.DatetimeIndex(e.index).tz_localize(None) if pd.DatetimeIndex(e.index).tz is not None else e.index
    last = e.resample(freq).last().dropna()
    first_val = e.iloc[0]
    return last.pct_change().fillna(last.iloc[0] / first_val - 1)


def regime_metrics(equity: pd.Series, regime: pd.Series, bars_per_year: int = 252) -> dict:
    """Return statistics conditional on the regime label known at each bar."""
    r = equity.pct_change().dropna()
    reg = regime.reindex(r.index)
    out = {}
    for lab, g in r.groupby(reg):
        if len(g) < 2:
            continue
        sd = g.std(ddof=1)
        out[str(lab)] = {"n_bars": int(len(g)), "ann_return": float(g.mean() * bars_per_year),
                         "sharpe": float(g.mean() / sd * np.sqrt(bars_per_year)) if sd > 0 else np.nan,
                         "hit_rate": float((g > 0).mean())}
    return out
