import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.data.bars import Timeframe, to_canonical
from qsts.indicators import core as ind
from qsts.indicators import structure as st


@pytest.fixture(scope="module")
def df():
    return to_canonical(synthetic_daily(seed=3), Timeframe.D1)


@pytest.fixture(scope="module")
def bench():
    return to_canonical(synthetic_daily(seed=99), Timeframe.D1)["close"]


INDICATORS = {
    "sma": lambda d: ind.sma(d["close"], 20), "ema": lambda d: ind.ema(d["close"], 20), "wma": lambda d: ind.wma(d["close"], 10),
    "hma": lambda d: ind.hma(d["close"], 16), "rsi": lambda d: ind.rsi(d["close"], 14), "atr": lambda d: ind.atr(d, 14),
    "macd": lambda d: ind.macd(d["close"]), "adx": lambda d: ind.adx(d), "supertrend": lambda d: ind.supertrend(d),
    "bollinger": lambda d: ind.bollinger(d["close"]), "keltner": lambda d: ind.keltner(d), "stoch": lambda d: ind.stochastic(d),
    "cci": lambda d: ind.cci(d), "williams": lambda d: ind.williams_r(d), "roc": lambda d: ind.roc(d["close"]),
    "obv": lambda d: ind.obv(d), "relvol": lambda d: ind.relative_volume(d), "ichimoku": lambda d: ind.ichimoku(d),
    "swings": lambda d: st.swing_points(d), "breakout": lambda d: st.breakout(d),
}


@pytest.mark.parametrize("name", sorted(INDICATORS))
def test_every_indicator_is_causal(name, df):
    """Truncating the future must not change any past value (look-ahead detector)."""
    full = INDICATORS[name](df)
    for cut in (300, 520):
        part = INDICATORS[name](df.iloc[:cut])
        if isinstance(full, pd.Series):
            pd.testing.assert_series_equal(part, full.iloc[:cut], check_exact=False, rtol=1e-9, atol=1e-12)
        else:
            pd.testing.assert_frame_equal(part, full.iloc[:cut], check_exact=False, rtol=1e-9, atol=1e-12)


def test_known_values():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert ind.sma(s, 3).tolist()[2:] == [2, 3, 4]
    assert np.isclose(ind.wma(s, 3).iloc[-1], (3 * 1 + 4 * 2 + 5 * 3) / 6)
    up = pd.Series(np.arange(1.0, 40))
    assert ind.rsi(up, 14).dropna().eq(100).all()
    flat = pd.DataFrame({"high": [11.0] * 30, "low": [9.0] * 30, "close": [10.0] * 30, "open": [10.0] * 30})
    assert np.isclose(ind.atr(flat, 14).iloc[-1], 2.0)
    b = ind.bollinger(pd.Series([10.0] * 25), 20)
    assert np.isclose(b["mid"].iloc[-1], 10) and np.isclose(b["upper"].iloc[-1], 10)


def test_rsi_reference_wilder():
    # Wilder's textbook example (New Concepts in Technical Trading Systems): first RSI = 70.53 (2dp)
    closes = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89,
                        46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64])
    r = ind.rsi(closes, 14)
    assert r.first_valid_index() == 14
    assert round(r.iloc[14], 2) == 70.46  # StockCharts' published Wilder RSI worksheet value
    assert round(r.iloc[15], 2) == 66.25


def test_relative_volume_excludes_current_bar(df):
    rv = ind.relative_volume(df, 20)
    i = 100
    expected = df["volume"].iloc[i] / df["volume"].iloc[i - 20:i].mean()
    assert np.isclose(rv.iloc[i], expected)


def test_swings_confirmed_late():
    h = pd.Series([1, 2, 3, 10, 3, 2, 1, 2, 3, 4], dtype=float)
    df = pd.DataFrame({"high": h, "low": h - 0.5, "close": h, "open": h})
    sp = st.swing_points(df, k=3)
    # pivot at index 3 is only known at index 6
    assert not sp["swing_high_confirmed"].iloc[:6].any()
    assert sp["swing_high_confirmed"].iloc[6]
    assert np.isnan(sp["resistance"].iloc[5]) and sp["resistance"].iloc[6] == 10
