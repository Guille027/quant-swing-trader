"""Strategy audit ("Edge check"): is the backtest result an edge or an accident?

A published strategy usually comes with its best-looking chart. These checks look for the usual ways a backtest
fools you: too few trades, only one lucky period, only one lucky stock, settings tuned to the past, costs, luck in
the order of the trades, and simply holding the stock being better. Each check says what it measured; none of them
uses data the strategy could not have had at the time (signals stay causal).
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from qsts.lab import metrics
from qsts.lab.backtest import BacktestConfig, buy_and_hold, run_backtest
from qsts.lab.strategy import Strategy

MIN_TRADES = 30


def _p(x: float) -> str:
    """+12,7% (Spanish decimals)."""
    return f"{x:+.1%}".replace(".", ",")


def _d(x: float, nd: int = 2) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def _net(res) -> float:
    return float(res.equity.iloc[-1] / res.config.initial_capital - 1) if len(res.equity) else 0.0


def _sharpe(eq: pd.Series) -> float | None:
    r = eq.pct_change().dropna()
    return float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if len(r) > 2 and r.std(ddof=1) > 0 else None


def audit(strategy: Strategy, bars: pd.DataFrame, params: dict | None, cfg: BacktestConfig,
          basket: dict[str, pd.DataFrame] | None = None, n_tested: int = 1) -> dict:
    base = run_backtest(strategy, bars, params, cfg)
    eq, tr = base.equity, base.trades
    bh = buy_and_hold(bars, eq.index[0], eq.index[-1], cfg.initial_capital) if len(eq) else eq
    checks = []

    def add(key, label, ok, detail, **data):
        checks.append({"key": key, "label": label, "ok": ok, "detail": detail, **data})

    n = len(tr)
    add("trades", "Suficientes operaciones", n >= MIN_TRADES,
        f"{n} operaciones (mínimo {MIN_TRADES} para que las estadísticas signifiquen algo).", value=n)

    s_strat, s_bh = _sharpe(eq), _sharpe(bh)
    add("beats_hold", "Mejor rentabilidad/riesgo que mantener la acción",
        None if s_strat is None or s_bh is None else s_strat >= s_bh,
        f"Sharpe {_d(s_strat)} frente a {_d(s_bh)} de comprar y mantener." if s_strat is not None and s_bh is not None
        else "No se puede calcular.", strategy=s_strat, hold=s_bh)

    costly = run_backtest(strategy, bars, params, replace(cfg, slippage_bps=cfg.slippage_bps * 2 + 5,
                                                           commission_pct=cfg.commission_pct + 0.05))
    add("costs", "Sigue ganando con costes más altos", _net(costly) > 0,
        f"Con el doble de deslizamiento y 0,05% de comisión: {_p(_net(costly))} (sin ellos: {_p(_net(base))}).",
        value=_net(costly))

    if len(eq) > 40:  # each half of the history on its own
        mid = eq.index[len(eq) // 2]
        h1 = run_backtest(strategy, bars, params, replace(cfg, start=str(eq.index[0].date()), end=str(mid.date())))
        h2 = run_backtest(strategy, bars, params, replace(cfg, start=str(mid.date()), end=str(eq.index[-1].date())))
        add("halves", "Gana en las dos mitades del periodo", _net(h1) > 0 and _net(h2) > 0,
            f"Primera mitad (hasta {mid.date()}): {_p(_net(h1))} · segunda mitad: {_p(_net(h2))}.",
            first=_net(h1), second=_net(h2))

    yrs = metrics.yearly(eq)
    if len(yrs) >= 3:
        pos = sum(1 for y in yrs if (y["strategy"] or 0) > 0) / len(yrs)
        add("years", "Gana en la mayoría de los años", pos >= 0.6,
            f"{pos:.0%} de los {len(yrs)} años en positivo.", value=pos)

    if strategy.param_grid:  # one setting changed at a time, the others as published
        neigh = []
        for k, values in strategy.param_grid.items():
            for v in values:
                p = {**(params or {}), k: v}
                if strategy.resolve(p) == strategy.resolve(params):
                    continue
                r = run_backtest(strategy, bars, p, cfg)
                neigh.append({"param": k, "value": v, "net": _net(r), "sharpe": _sharpe(r.equity)})
        if neigh:
            good = [x for x in neigh if x["net"] > 0 and (s_strat is None or s_strat <= 0
                                                          or (x["sharpe"] or -9) >= 0.5 * s_strat)]
            share = len(good) / len(neigh)
            add("robust", "Aguanta cambios en sus ajustes", share >= 0.6,
                f"{len(good)} de {len(neigh)} variantes cercanas siguen ganando y conservan al menos la mitad del Sharpe.",
                value=share, variants=neigh)

    if basket:  # the same rules on other stocks: an edge rarely lives in a single ticker
        rows = []
        for sym, b in basket.items():
            try:
                r = run_backtest(strategy, b, params, cfg)
            except (ValueError, KeyError):
                continue
            if len(r.equity) < 50:
                continue
            st = metrics.trade_stats(r.trades)
            hold = buy_and_hold(b, r.equity.index[0], r.equity.index[-1])
            rows.append({"symbol": sym, "net": _net(r), "profit_factor": st["profit_factor"], "trades": st["n_trades"],
                         "hold": float(hold.iloc[-1] / hold.iloc[0] - 1) if len(hold) else None})
        if rows:
            pos = sum(1 for x in rows if (x["profit_factor"] or 0) > 1) / len(rows)
            add("others", "Funciona también en otras acciones", pos >= 0.6,
                f"Beneficio (factor de beneficio > 1) en {pos:.0%} de {len(rows)} acciones y ETF líquidos; "
                f"mediana {_p(float(np.median([x['net'] for x in rows])))}.", value=pos, symbols=sorted(rows, key=lambda x: -x["net"]))

    mc = metrics.monte_carlo(tr)
    if mc:
        add("luck", "Resiste la mala suerte (Monte Carlo)", (mc["final"]["5"] or -1) > 0,
            f"Reordenando y repitiendo sus operaciones 1.000 veces: en el 5% peor de los casos termina en "
            f"{_p(mc['final']['5'])}; probabilidad de acabar perdiendo {mc['p_loss']:.0%}.", value=mc["final"]["5"])

    rets = eq.pct_change().dropna()
    bh_r = bh.pct_change().dropna()
    bench_sr = max(0.0, float(bh_r.mean() / bh_r.std(ddof=1))) if len(bh_r) > 2 and bh_r.std(ddof=1) > 0 else 0.0
    p = metrics.psr(rets, bench_sr)
    add("psr", "Probablemente no es suerte", None if p is None else p >= 0.9,
        (f"Probabilidad de que su rentabilidad/riesgo real supere a mantener la acción: {p:.0%} (se pide 90%). "
         f"Ojo: llevas {n_tested} estrategias probadas en la app; cuantas más pruebas, más fácil que alguna salga "
         "bien por casualidad.") if p is not None else "Demasiados pocos datos.", value=p)

    decided = [c for c in checks if c["ok"] is not None]
    passed = sum(1 for c in decided if c["ok"])
    return {"checks": checks, "passed": passed, "total": len(decided),
            "verdict": ("sólida" if passed == len(decided) else "dudosa" if passed >= len(decided) * 0.6 else "frágil")}
