"""Portfolio bots: a strategy as a scanner over many stocks, at most N positions. All prices are SYNTHETIC."""
import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.data.bars import Timeframe, nyse_sessions, to_canonical
from qsts.lab.audit import audit_portfolio
from qsts.lab.backtest import BacktestConfig, run_backtest
from qsts.lab.portfolio import PanelBuilder, PortfolioConfig, breadth_summary, candidates, monkey_spec, run_portfolio
from qsts.lab.service import LabService, NotReady
from qsts.lab.strategy import REGISTRY, Strategy, get

CAL = nyse_sessions("2024-01-02", "2024-03-29")


class Plan(Strategy):
    """Test strategy: per-stock signals written in `plan` {symbol: {row number: {column: value}}}; rank = `ranks`."""
    key, name = "test_plan", "Plan de prueba"
    default_symbols = ()
    universe = False
    allow_short = True
    plan: dict = {}
    ranks: dict = {}

    def signals(self, bars, p):
        out = pd.DataFrame({"entry": 0, "exit": False}, index=bars.index)
        for i, row in self.plan.get(bars.attrs.get("symbol"), {}).items():
            for k, v in row.items():
                if k not in out:
                    out[k] = np.nan
                out.iloc[i, out.columns.get_loc(k)] = v
        return out

    def rank(self, bars, p):
        return pd.Series(self.ranks.get(bars.attrs.get("symbol"), 0.0), index=bars.index, dtype=float)


def flat(symbol, price=100.0, n=len(CAL)):
    df = pd.DataFrame({"open": price, "high": price * 1.01, "low": price * 0.99, "close": price, "volume": 1e6},
                      index=CAL[:n])
    df = to_canonical(df, Timeframe.D1)
    df.attrs["symbol"] = symbol
    return df


def panel_of(frames: dict, strategy, starts=None, params=None):
    pb = PanelBuilder(CAL, sorted(frames))
    for s in sorted(frames):
        pb.add(s, frames[s], strategy.run(frames[s], params), (starts or {}).get(s))
    return pb.done()


def test_free_places_go_to_the_best_ranked_signals():
    st = Plan()
    Plan.plan = {s: {2: {"entry": 1}} for s in "ABCD"}
    Plan.ranks = {"A": 1.0, "B": 4.0, "C": 3.0, "D": 2.0}
    frames = {s: flat(s, 100.0 + i) for i, s in enumerate("ABCD")}
    res = run_portfolio(panel_of(frames, st), PortfolioConfig(initial_capital=10_000, max_positions=2, slippage_bps=0))
    assert sorted(t["symbol"] for t in res.open_trades) == ["B", "C"]  # rank 4 and 3; A and D get no place
    for t in res.open_trades:  # bought at the next open, each with half of the equity
        assert t["entry_time"] == CAL[3] and t["qty"] * t["entry_price"] == pytest.approx(5_000)
    assert res.position.max() == 2


def test_exits_free_places_and_a_stock_is_only_traded_once_in_the_index():
    st = Plan()
    Plan.plan = {"A": {2: {"entry": 1}, 5: {"exit": True}}, "B": {2: {"entry": 1}, 6: {"entry": 1}},
                 "C": {2: {"entry": 1}}}
    Plan.ranks = {"A": 3.0, "B": 2.0, "C": 1.0}
    frames = {s: flat(s) for s in "ABC"}
    cfg = PortfolioConfig(initial_capital=10_000, max_positions=1, slippage_bps=0)
    res = run_portfolio(panel_of(frames, st), cfg)
    t = res.trades.iloc[0]
    assert t["symbol"] == "A" and t["entry_time"] == CAL[3] and t["exit_time"] == CAL[6]
    assert [x["symbol"] for x in res.open_trades] == ["B"] and res.open_trades[0]["entry_time"] == CAL[7]
    # B only joined the index on row 10: its earlier signals are ignored
    res = run_portfolio(panel_of(frames, st, starts={"B": CAL[10]}), cfg)
    assert [x["symbol"] for x in res.open_trades] == [] and list(res.trades["symbol"]) == ["A"]


def test_one_stock_one_place_equals_the_single_stock_backtest():
    """The portfolio engine with one stock and one place is the single-stock backtester (fills, stops, shorts)."""
    for key in ("connors_rsi2", "turtle_20_10"):
        st = get(key)
        bars = to_canonical(synthetic_daily("2015-01-01", "2019-12-31", seed=3), Timeframe.D1)
        single = run_backtest(st, bars, cfg=BacktestConfig(initial_capital=10_000))
        pb = PanelBuilder(bars.index, ["X"])
        pb.add("X", bars, st.run(bars), None)
        port = run_portfolio(pb.done(), PortfolioConfig(initial_capital=10_000, max_positions=1))
        assert len(port.trades) == len(single.trades) > 5
        np.testing.assert_allclose(port.trades["pnl"], single.trades["pnl"], rtol=1e-9)
        np.testing.assert_allclose(port.equity.to_numpy(), single.equity.to_numpy(), rtol=1e-9)


def test_never_leveraged_and_monkeys_and_audit():
    st = get("connors_rsi2")
    frames = {}
    for i in range(12):
        b = to_canonical(synthetic_daily("2012-01-01", "2019-12-31", seed=40 + i), Timeframe.D1)
        frames[f"S{i:02d}"] = b
    pb = PanelBuilder(frames["S00"].index, sorted(frames))
    rows = []
    for s, b in frames.items():
        pb.add(s, b, st.run(b), None)
        rows.append(LabService._one_stock(s, b, st.run(b), None))
    panel = pb.done()
    cfg = PortfolioConfig(initial_capital=10_000, max_positions=5)
    res = run_portfolio(panel, cfg)
    assert len(res.trades) > 50 and res.position.max() <= 5
    tr = res.trades
    for day in res.equity.index[::50]:  # money invested never exceeds the equity
        on = tr[(tr["entry_time"] <= day) & (tr["exit_time"] > day)]
        assert (on["qty"] * on["entry_price"]).sum() <= res.equity.asof(day) * 1.2
    spec = monkey_spec(panel, tr)
    m1 = run_portfolio(panel, cfg, monkey={**spec, "seed": 1})
    assert len(m1.trades) > 20 and set(m1.trades["reason"]) <= {"signal", "end", "data_end"}
    b = breadth_summary(rows)
    assert b["n_used"] == 12 and 0 <= b["profitable"] <= 1
    bench = frames["S00"]["close"]
    out = audit_portfolio(panel, cfg, bench, b, n_tested=3)
    keys = {c["key"] for c in out["checks"]}
    assert {"trades", "beats_hold", "costs", "halves", "breadth", "split", "monkey", "psr"} <= keys
    assert out["verdict"] in ("sólida", "dudosa", "frágil")
    assert candidates(panel, len(panel.index) - 1) is not None


@pytest.fixture
def ulab(sf, tmp_path):
    frames = {f"S{i:02d}": to_canonical(synthetic_daily("2016-01-01", "2020-12-31", seed=60 + i), Timeframe.D1)
              for i in range(6)}
    frames["SPY"] = to_canonical(synthetic_daily("2016-01-01", "2020-12-31", seed=99), Timeframe.D1)
    starts = {s: None for s in frames if s != "SPY"}
    starts["S05"] = pd.Timestamp("2019-01-02").date()

    def universe():
        return {"symbols": starts, "dated": True, "first": {s: frames[s].index[0] for s in starts},
                "last": {s: frames[s].index[-1] for s in starts}}

    def frame(s):
        if s not in frames:
            raise KeyError(s)
        return frames[s]
    lab = LabService(sf, frame, lambda s: (1,), universe=universe, data_version=lambda: (1,),
                     cache_dir=tmp_path / "cache")
    lab.sync()
    return lab


def test_portfolio_bot_is_computed_in_the_background_and_cached(ulab, tmp_path):
    b = ulab.get_bot("connors_rsi2-sp500")
    assert b.kind == "universe" and b.max_positions == 5
    row = ulab.row(b)
    assert "computing" in row
    ulab.wait()
    row = ulab.row(b)
    assert row["symbol"] == "S&P 500" and row["n_trades"] > 0 and row["kind"] == "universe"
    d = ulab.detail(b.id)
    assert d["breadth"]["summary"]["n_symbols"] == 6 and d["universe"]["dated"]
    assert {t["symbol"] for t in d["trades"]} <= {f"S{i:02d}" for i in range(6)}
    s05 = [t for t in d["trades"] if t["symbol"] == "S05"]
    assert all(t["entry"] >= "2019-01-02" for t in s05)  # traded only once in the index
    assert d["next"]["free"] <= 5 and d["config"]["max_positions"] == 5
    # computed once: a new service (the app restarted) reads it from disk
    again = LabService(ulab.sf, ulab.frame, ulab.token, universe=ulab.universe_source,
                       data_version=ulab.data_version, cache_dir=tmp_path / "cache")
    assert again.urun(b).key == ulab.urun(b).key
    with pytest.raises(ValueError):
        ulab.create_bot("connors_rsi2", "SP500", max_positions=9)  # the user's limit: 5
    three = ulab.create_bot("connors_rsi2", "SP500", max_positions=3)
    assert three.id == "connors_rsi2-sp500-3pos" and three.max_positions == 3
    with pytest.raises(NotReady):
        ulab.urun(three)
    ulab.wait()
    assert ulab.urun(three).result.position.max() <= 3


def test_portfolio_audit_runs_in_the_background(ulab):
    b = ulab.get_bot("golden_cross-sp500")
    assert "computing" in ulab.audit(b.id)
    ulab.wait()
    a = ulab.audit(b.id)
    keys = {c["key"] for c in a["checks"]}
    assert {"split", "breadth", "robust", "costs"} <= keys and a["verdict"]
