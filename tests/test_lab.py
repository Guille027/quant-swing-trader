"""Strategy lab: backtester fills, metrics, causality of every strategy, the library service.
All prices are SYNTHETIC (hand-made or random walks), never market data."""
import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.data.bars import Timeframe, nyse_sessions, to_canonical
from qsts.lab import metrics
from qsts.lab.backtest import BacktestConfig, buy_and_hold, run_backtest
from qsts.lab.strategy import REGISTRY, Strategy, load_all


class Scripted(Strategy):
    """Test helper: replays a hand-written signal table."""
    key, name = "scripted", "scripted"
    allow_short = True

    def __init__(self, table: dict):
        self.table = table

    def signals(self, bars, p):
        out = pd.DataFrame(index=bars.index)
        for col, values in self.table.items():
            out[col] = values
        return out


def bars_from(rows, start="2024-01-02"):
    """rows: (open, high, low, close) per session."""
    idx = nyse_sessions(start, "2024-12-31")[:len(rows)]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1e6)
    return to_canonical(df, Timeframe.D1)


NOCOST = BacktestConfig(initial_capital=10_000, slippage_bps=0, commission_pct=0)
FLAT = [(100, 101, 99, 100)] * 3


def test_signal_at_close_fills_at_next_open():
    rows = FLAT + [(102, 105, 101, 104), (104, 108, 103, 107), (110, 111, 109, 110), (110, 110, 110, 110)]
    entry = [0, 0, 1, 0, 0, 0, 0]   # decided at the close of day 3 -> bought at day 4's open (102)
    exit_ = [0, 0, 0, 0, 1, 0, 0]   # decided at the close of day 5 -> sold at day 6's open (110)
    res = run_backtest(Scripted({"entry": entry, "exit": exit_}), bars_from(rows), cfg=NOCOST)
    t = res.trades.iloc[0]
    assert len(res.trades) == 1 and t["entry_price"] == 102 and t["exit_price"] == 110 and t["reason"] == "signal"
    assert t["qty"] == pytest.approx(10_000 / 102) and t["pnl"] == pytest.approx(10_000 / 102 * 8)
    assert res.equity.iloc[3] == pytest.approx(10_000 / 102 * 104)  # marked at the close
    assert res.equity.iloc[-1] == pytest.approx(10_000 * 110 / 102)
    # costs: slippage on both market fills and commission on both sides
    cfg = BacktestConfig(initial_capital=10_000, slippage_bps=10, commission_pct=0.1)
    c = run_backtest(Scripted({"entry": entry, "exit": exit_}), bars_from(rows), cfg=cfg).trades.iloc[0]
    buy, sell = 102 * 1.001, 110 * 0.999
    q = 10_000 / buy
    assert c["entry_price"] == pytest.approx(buy) and c["exit_price"] == pytest.approx(sell)
    assert c["pnl"] == pytest.approx(q * (sell - buy) - q * buy * 0.001 - q * sell * 0.001)


def test_stops_and_targets_are_conservative():
    base = FLAT + [(100, 101, 99, 100)]
    # stop 5% below the 100 entry: touched inside day 5 -> filled at 95
    rows = base + [(100, 100, 94, 96), (96, 97, 95, 96)]
    sig = {"entry": [0, 0, 1, 0, 0, 0], "stop_pct": [np.nan, np.nan, 0.05, np.nan, np.nan, np.nan]}
    t = run_backtest(Scripted(sig), bars_from(rows), cfg=NOCOST).trades.iloc[0]
    assert t["exit_price"] == pytest.approx(95) and t["reason"] == "stop"
    # gap below the stop at the open -> filled at the (worse) open
    rows = base + [(90, 92, 89, 91), (91, 92, 90, 91)]
    t = run_backtest(Scripted(sig), bars_from(rows), cfg=NOCOST).trades.iloc[0]
    assert t["exit_price"] == 90 and t["reason"] == "stop_gap"
    # stop and target touched in the same bar: the stop is assumed first
    both = {**sig, "target_pct": [np.nan, np.nan, 0.05, np.nan, np.nan, np.nan]}
    rows = base + [(100, 106, 94, 100), (100, 100, 100, 100)]
    t = run_backtest(Scripted(both), bars_from(rows), cfg=NOCOST).trades.iloc[0]
    assert t["reason"] == "stop" and t["exit_price"] == pytest.approx(95)
    # target only: limit filled at the target; a gap above it fills at the (better) open
    rows = base + [(100, 106, 99, 104), (104, 104, 104, 104)]
    t = run_backtest(Scripted(both), bars_from(rows), cfg=NOCOST).trades.iloc[0]
    assert t["reason"] == "target" and t["exit_price"] == pytest.approx(105)
    rows = base + [(108, 109, 107, 108), (108, 108, 108, 108)]
    t = run_backtest(Scripted(both), bars_from(rows), cfg=NOCOST).trades.iloc[0]
    assert t["reason"] == "target_gap" and t["exit_price"] == 108
    # the entry bar itself can hit the stop (the stop exists from the fill on)
    rows = FLAT + [(100, 100, 94, 95), (95, 95, 95, 95)]
    sig1 = {"entry": [0, 0, 1, 0, 0], "stop_pct": [np.nan, np.nan, 0.05, np.nan, np.nan]}
    t = run_backtest(Scripted(sig1), bars_from(rows), cfg=NOCOST).trades.iloc[0]
    assert t["reason"] == "stop" and t["bars"] == 0


def test_shorts_reversals_and_trailing_stop():
    rows = FLAT + [(100, 100, 90, 92), (92, 93, 88, 90), (90, 95, 89, 94), (94, 96, 93, 95), (95, 95, 95, 95)]
    # short at day 4's open (100), reversed to long at day 6's open (90) by an opposite entry
    sig = {"entry": [0, 0, -1, 0, 1, 0, 0, 0]}
    res = run_backtest(Scripted(sig), bars_from(rows), cfg=NOCOST)
    s, lg = res.trades.iloc[0], res.trades.iloc[1] if len(res.trades) > 1 else None
    assert s["side"] == "corto" and s["exit_price"] == 90 and s["reason"] == "reverse"
    assert s["pnl"] == pytest.approx(10_000 / 100 * 10)
    assert res.open_trade is not None and res.open_trade["side"] == "largo" and res.open_trade["entry_price"] == 90
    assert lg is None
    # trailing stop: only moves up for a long
    rows = FLAT + [(100, 105, 99, 104), (104, 110, 103, 109), (109, 109, 101, 102), (102, 102, 102, 102)]
    trail = [np.nan, np.nan, np.nan, 98, 104, 90, np.nan]  # 104 set at day 5's close; 90 would lower it: ignored
    t = run_backtest(Scripted({"entry": [0, 0, 1, 0, 0, 0, 0], "trail": trail}), bars_from(rows), cfg=NOCOST).trades.iloc[0]
    assert t["reason"] == "stop" and t["exit_price"] == 104


def test_no_leverage_and_partial_size():
    with pytest.raises(ValueError):
        BacktestConfig(size_pct=150)
    rows = FLAT + [(100, 100, 100, 100), (110, 110, 110, 110), (110, 110, 110, 110)]
    sig = {"entry": [0, 0, 1, 0, 0, 0], "exit": [0, 0, 0, 1, 0, 0]}
    half = run_backtest(Scripted(sig), bars_from(rows), cfg=BacktestConfig(size_pct=50, slippage_bps=0))
    assert half.trades.iloc[0]["qty"] == pytest.approx(50) and half.equity.iloc[-1] == pytest.approx(10_500)


def test_strategy_contract_is_enforced():
    rows = FLAT * 2
    with pytest.raises(ValueError, match="shorts"):
        class LongOnly(Scripted):
            allow_short = False
        run_backtest(LongOnly({"entry": [0, -1, 0, 0, 0, 0]}), bars_from(rows))
    with pytest.raises(ValueError, match="unknown signal columns"):
        run_backtest(Scripted({"buy": [0] * 6}), bars_from(rows))


def test_every_strategy_is_causal():
    """No strategy may use the future: signals computed on a history cut at any day equal the full-history signals
    up to that day. Runs for EVERY registered strategy (new ones are covered automatically)."""
    load_all()
    assert len(REGISTRY) >= 3
    bars = to_canonical(synthetic_daily("2015-01-01", "2020-12-31", seed=11), Timeframe.D1)
    for key, st in REGISTRY.items():
        full = st.run(bars)
        for cut in (300, 700, 1200):
            part = st.run(bars.iloc[:cut])
            pd.testing.assert_frame_equal(full.iloc[:cut], part, check_exact=False, rtol=1e-9, obj=key)


def test_metrics_on_a_known_curve():
    idx = nyse_sessions("2023-01-03", "2023-12-29")
    eq = pd.Series(np.linspace(10_000, 12_000, len(idx)), index=idx)
    eq.iloc[100:110] = eq.iloc[99] * 0.9  # one 10% dip
    tr = pd.DataFrame({"side": ["largo"] * 4 + ["corto"], "pnl": [100.0, -50.0, 200.0, -50.0, 30.0],
                       "pnl_pct": [0.01, -0.005, 0.02, -0.005, 0.003], "return": [0.01, -0.005, 0.02, -0.005, 0.003],
                       "bars": [3, 2, 5, 1, 2], "entry_time": idx[:5], "exit_time": idx[5:10]})
    sm = metrics.summary(eq, tr, 10_000)
    assert sm["net_profit_pct"] == pytest.approx(0.2) and sm["n_trades"] == 5
    assert sm["win_rate"] == pytest.approx(0.6) and sm["profit_factor"] == pytest.approx(330 / 100)
    assert sm["max_drawdown"] == pytest.approx(-0.1, abs=1e-3)
    st = metrics.trade_stats(tr)
    assert st["max_consec_wins"] == 1 and st["payoff"] == pytest.approx((330 / 3) / 50)
    side = metrics.report_by_side(tr, 10_000)
    assert side["long"]["n_trades"] == 4 and side["short"]["net_profit"] == pytest.approx(30)
    km = metrics.key_metrics(eq, tr)
    assert km["longest_dd_days"] >= 14 and km["sharpe"] > 0 and km["ulcer"] > 0
    months = metrics.monthly_returns(eq)
    assert months[0]["year"] == 2023 and months[0]["total"] == pytest.approx(0.2)
    assert metrics.window_return(eq, 30) == pytest.approx(eq.iloc[-1] / eq[eq.index <= eq.index[-1] - pd.Timedelta(days=30)].iloc[-1] - 1)
    mc = metrics.monte_carlo(pd.DataFrame({"return": [0.01, -0.005] * 10}), n_sims=200)
    assert mc["final"]["5"] <= mc["final"]["50"] <= mc["final"]["95"] and 0 <= mc["p_loss"] <= 1
    assert metrics.monte_carlo(tr) is None  # too few trades to say anything


def test_classic_strategies_run_and_buy_and_hold():
    bars = to_canonical(synthetic_daily("2012-01-01", "2022-12-30", seed=5), Timeframe.D1)
    load_all()
    for key in ("connors_rsi2", "golden_cross", "turtle_20_10"):
        res = run_backtest(REGISTRY[key], bars)
        assert len(res.equity) == len(bars) and (res.equity > 0).all()
        assert res.trades["exit_time"].ge(res.trades["entry_time"]).all()
    bh = buy_and_hold(bars, capital=10_000)
    assert bh.iloc[0] == 10_000 and bh.iloc[-1] == pytest.approx(10_000 * bars["close"].iloc[-1] / bars["close"].iloc[0])


# ---------------------------------------------------------------------- library service
@pytest.fixture
def lab(sf):
    from qsts.lab.service import LabService
    frames = {s: to_canonical(synthetic_daily("2014-01-01", "2024-12-31", seed=i), Timeframe.D1)
              for i, s in enumerate(["SPY", "QQQ", "GLD", "AAA", "BBB"])}

    def frame(sym):
        if sym not in frames:
            raise KeyError(sym)
        return frames[sym]
    return LabService(sf, frame, lambda s: ("v1",), basket=lambda: ["AAA", "BBB", "QQQ"])


def test_library_bots_detail_and_audit(lab):
    assert lab.sync() >= 6 and lab.sync() == 0  # defaults created once
    lib = lab.library()
    ids = {r["id"] for r in lib["rows"]}
    assert "connors_rsi2-spy" in ids and lib["n_strategies"] >= 3
    row = next(r for r in lib["rows"] if r["id"] == "connors_rsi2-spy")
    assert row["timeframe"] == "1 día" and len(row["spark"]) > 10 and row["n_trades"] > 0 and "d90" in row
    d = lab.detail("connors_rsi2-spy")
    assert d["bot"]["strategy"]["name"].startswith("RSI(2)") and d["equity"] and d["hold"] and d["trades"]
    assert set(d["by_side"]) == {"all", "long", "short"} and d["monthly"] and d["key_metrics"]["sharpe"] is not None
    assert d["pnl_range"]["hold"]["max"] >= d["pnl_range"]["hold"]["current"]
    a = lab.audit("connors_rsi2-spy")
    keys = {c["key"] for c in a["checks"]}
    assert {"trades", "beats_hold", "costs", "halves", "robust", "others", "psr"} <= keys
    assert a["verdict"] in ("sólida", "dudosa", "frágil")
    # a new bot: another stock and a custom parameter
    b = lab.create_bot("connors_rsi2", "aaa", {"rsi_buy": 5.0})
    assert b.id == "connors_rsi2-aaa-rsi_buy5.0" and b.params == {"rsi_buy": 5.0}
    with pytest.raises(ValueError):
        lab.create_bot("connors_rsi2", "AAA", {"nope": 1})
    with pytest.raises(KeyError):
        lab.create_bot("nope", "AAA")
    missing = lab.create_bot("golden_cross", "ZZZ")
    assert "sin datos" in lab.row(missing)["error"]
    lab.update_bot("golden_cross-zzz", hidden=True)
    assert "golden_cross-zzz" not in {r["id"] for r in lab.library()["rows"]}
    lab.sync()  # a removed bot is not created again
    assert lab.get_bot("golden_cross-zzz").hidden


def test_stop_and_limit_on_open_entries_and_day_trades():
    base = FLAT + [(100, 101, 99, 100)]
    nan = np.nan
    # signal at the close of day 4; buy stop at 103: day 5 opens 101 and trades up to 104 -> filled at 103; day trade: out at that day's close (102)
    sig = {"entry": [0, 0, 0, 1, 0], "entry_stop": [nan, nan, nan, 103, nan]}
    rows = base + [(101, 104, 100.5, 102)]
    st = Scripted(sig)
    st.day_trade = True
    t = run_backtest(st, bars_from(rows), cfg=NOCOST).trades.iloc[0]
    assert t["entry_price"] == 103 and t["exit_price"] == 102 and t["reason"] == "close" and t["bars"] == 0
    # the open gaps above the stop: filled at the open; never reached: no trade
    r = run_backtest(Scripted(sig), bars_from(base + [(105, 106, 104, 105)]), cfg=NOCOST)
    assert r.open_trade["entry_price"] == 105
    r = run_backtest(Scripted(sig), bars_from(base + [(101, 102, 100, 101)]), cfg=NOCOST)
    assert r.trades.empty and r.open_trade is None
    # limit-on-open at 99: only if it OPENS at or below 99 (later in the day does not count)
    lim = {"entry": [0, 0, 0, 1, 0], "entry_limit": [nan, nan, nan, 99, nan]}
    assert run_backtest(Scripted(lim), bars_from(base + [(100, 101, 97, 98)]), cfg=NOCOST).open_trade is None
    assert run_backtest(Scripted(lim), bars_from(base + [(98, 101, 97, 100)]), cfg=NOCOST).open_trade["entry_price"] == 98


def test_intraday_strategies_match_between_engines():
    from qsts.lab.portfolio import PanelBuilder, PortfolioConfig, run_portfolio
    bars = to_canonical(synthetic_daily("2015-01-01", "2019-12-31", seed=8), Timeframe.D1)
    for key in ("nr7_breakout", "gap_down_fill", "turnaround_tuesday"):
        st = REGISTRY[key]
        single = run_backtest(st, bars, cfg=BacktestConfig(initial_capital=10_000))
        pb = PanelBuilder(bars.index, ["X"], day_trade=st.day_trade)
        pb.add("X", bars, st.run(bars), None)
        port = run_portfolio(pb.done(), PortfolioConfig(initial_capital=10_000, max_positions=1))
        assert len(single.trades) > 10 and (single.trades["bars"] == 0).all()
        np.testing.assert_allclose(port.trades["pnl"], single.trades["pnl"], rtol=1e-9)
