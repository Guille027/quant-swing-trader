"""Automatic paper trading against a FAKE Alpaca (no network). Prices are SYNTHETIC."""
import itertools

import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_daily
from qsts.broker.alpaca_paper import BrokerError
from qsts.data.bars import Timeframe, nyse_sessions, to_canonical
from qsts.lab.service import LabService
from qsts.lab.strategy import REGISTRY, Strategy
from qsts.lab.trader import PaperTrader, due_session

LAST = pd.Timestamp("2024-06-03", tz="UTC")       # last stored session (a Monday)
AFTER_CLOSE = pd.Timestamp("2024-06-03 21:30", tz="UTC")   # 20:00 UTC close + 45 min: the signal of June 3 is due
NEXT_DAY_OPEN = pd.Timestamp("2024-06-04 15:00", tz="UTC")


class FakeAlpaca:
    """Behaves like the adapter: orders are accepted, then filled by the test with fill()."""
    def __init__(self, equity=100_000.0):
        self.equity, self.is_open, self.shortable = equity, False, True
        self.orders, self.pos, self.cancelled = {}, {}, []
        self.ids = itertools.count(1)
        self.reject_gtc = False
        self.fractionable = True

    def account(self):
        return {"equity": self.equity, "cash": self.equity, "shorting_enabled": True, "trading_blocked": False,
                "account_blocked": False, "status": "ACTIVE", "currency": "USD"}

    def clock(self):
        return {"is_open": self.is_open, "next_open": "", "next_close": "", "timestamp": ""}

    def positions(self):
        return {s: {"qty": q} for s, q in self.pos.items() if q}

    def asset(self, symbol):
        return {"tradable": True, "shortable": self.shortable, "easy_to_borrow": self.shortable,
                "fractionable": self.fractionable}

    def submit(self, req):
        if self.reject_gtc and req.get("time_in_force") == "gtc":
            raise BrokerError("time_in_force gtc not allowed")
        oid = f"o{next(self.ids)}"
        o = {"id": oid, "client_order_id": req["client_order_id"], "symbol": req["symbol"], "side": req["side"],
             "qty": req["qty"], "filled_qty": 0, "filled_avg_price": None, "status": "accepted", "type": req["type"],
             "order_class": req.get("order_class", "simple"), "time_in_force": req["time_in_force"], "legs": [],
             "stop_price": req.get("stop_price"), "limit_price": req.get("limit_price"), "filled_at": None, "req": req}
        # bracket / oto legs close the entry (opposite side); an oco is itself an exit, its legs share its side
        exit_side = req["side"] if req.get("order_class") == "oco" else ("sell" if req["side"] == "buy" else "buy")
        if req.get("stop_loss"):
            o["legs"].append({"id": f"{oid}s", "side": exit_side, "qty": req["qty"], "type": "stop", "status": "held",
                              "stop_price": req["stop_loss"], "filled_qty": 0, "filled_avg_price": None, "filled_at": None})
        if req.get("take_profit") and req.get("order_class") != "oco":
            o["legs"].append({"id": f"{oid}t", "side": exit_side, "qty": req["qty"], "type": "limit", "status": "held",
                              "limit_price": req["take_profit"], "filled_qty": 0, "filled_avg_price": None, "filled_at": None})
        self.orders[oid] = o
        for leg in o["legs"]:
            self.orders[leg["id"]] = {**leg, "symbol": req["symbol"], "legs": []}
        return dict(o)

    def get_order(self, oid):
        o = dict(self.orders[oid])
        o["legs"] = [dict(self.orders[x["id"]]) for x in self.orders[oid].get("legs", [])]
        return o

    def cancel(self, oid):
        self.cancelled.append(oid)
        if self.orders[oid]["status"] not in ("filled",):
            self.orders[oid]["status"] = "canceled"

    def fill(self, oid, price, at="2024-06-04T13:30:00+00:00"):
        o = self.orders[oid]
        o.update(status="filled", filled_qty=o["qty"], filled_avg_price=price, filled_at=at)
        q = o["qty"] if o["side"] == "buy" else -o["qty"]
        self.pos[o["symbol"]] = self.pos.get(o["symbol"], 0) + q

    def last(self, **match):
        return [o for o in self.orders.values() if all(o.get(k) == v for k, v in match.items())][-1]


class Toggle(Strategy):
    """Test strategy: enters / exits on the dates listed in `plan` (module-level, edited by each test)."""
    key, name = "test_toggle", "Estrategia de prueba"
    default_symbols = ()
    allow_short = True
    plan: dict = {}

    def signals(self, bars, p):
        out = pd.DataFrame({"entry": 0, "exit": False}, index=bars.index)
        for day, row in self.plan.items():
            t = pd.Timestamp(day, tz="UTC")
            if t in out.index:
                for k, v in row.items():
                    out.loc[t, k] = v
        return out


@pytest.fixture
def world(sf):
    REGISTRY["test_toggle"] = Toggle()
    Toggle.plan = {}
    data = {"bars": to_canonical(synthetic_daily("2023-01-03", "2024-06-03", seed=1), Timeframe.D1), "v": 1}
    lab = LabService(sf, lambda s: data["bars"], lambda s: (data["v"],))
    broker, sent, refreshed = FakeAlpaca(), [], []

    class Tg:
        def send(self, text):
            sent.append(text)
    trader = PaperTrader(sf, lab, lambda: broker, lambda: Tg(), refresh=lambda syms: refreshed.append(syms) or True)
    bot = lab.create_bot("test_toggle", "SPY")
    yield {"lab": lab, "trader": trader, "broker": broker, "sent": sent, "bot": bot, "data": data,
           "refreshed": refreshed}
    REGISTRY.pop("test_toggle", None)


def set_plan(world, plan):
    """Change the test strategy's signals (bumps the data version so cached backtests are recomputed)."""
    Toggle.plan = plan
    world["data"]["v"] += 1


def add_day(world, day, close):
    """The vendor publishes one more daily bar."""
    bars = world["data"]["bars"]
    t = pd.Timestamp(day, tz="UTC")
    row = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6},
                       index=pd.DatetimeIndex([t], name="ts"))
    world["data"]["bars"] = pd.concat([bars, to_canonical(row, Timeframe.D1)])
    world["data"]["v"] += 1


def test_due_session_waits_for_the_close_and_the_delay():
    assert due_session(pd.Timestamp("2024-06-03 20:30", tz="UTC"), 45) == pd.Timestamp("2024-05-31", tz="UTC")
    assert due_session(AFTER_CLOSE, 45) == LAST
    assert due_session(pd.Timestamp("2024-06-08 12:00", tz="UTC"), 45) == pd.Timestamp("2024-06-07", tz="UTC")


def test_signal_to_order_to_fill_to_exit(world):
    tr, br, sent, bot = world["trader"], world["broker"], world["sent"], world["bot"]
    assert tr.tick(AFTER_CLOSE) == "sin bots en paper trading"
    with pytest.raises(ValueError):
        tr.activate(bot.id, 150)
    tr.activate(bot.id, 20, follow_open=False, now=AFTER_CLOSE)
    assert "activado" in sent[-1] and tr.lab.get_bot(bot.id).capital == pytest.approx(20_000)
    set_plan(world, {"2024-06-03": {"entry": 1}})
    close = float(world["data"]["bars"]["close"].iloc[-1])
    tr.tick(AFTER_CLOSE)
    entry = br.last(side="buy")
    # no stop / target: fractions of a share, the whole 20,000 invested as in the backtest
    assert entry["qty"] == pytest.approx(20_000 / close, abs=1e-4) and entry["qty"] != int(entry["qty"])
    assert entry["type"] == "market" and entry["time_in_force"] == "day"
    assert "COMPRA" in sent[-1] and "apertura" in sent[-1]
    n = len(br.orders)
    tr.tick(AFTER_CLOSE)
    assert len(br.orders) == n  # the same close is never handled twice
    # Alpaca fills it at the next open; the next run reads the fill back and reports it
    br.is_open = True
    br.fill(entry["id"], close * 1.01)
    tr.tick(NEXT_DAY_OPEN)
    assert "Ejecutada: compradas" in sent[-1]
    led = tr.ledger(tr.lab.get_bot(bot.id))
    assert led["qty"] == entry["qty"] and led["avg"] == pytest.approx(close * 1.01)
    # next close: exit signal -> sell order for the next open; filled -> result reported
    add_day(world, "2024-06-04", close * 1.05)
    set_plan(world, {"2024-06-03": {"entry": 1}, "2024-06-04": {"exit": True}})
    br.is_open = False
    tr.tick(pd.Timestamp("2024-06-04 21:30", tz="UTC"))
    ex = br.last(side="sell")
    assert ex["qty"] == entry["qty"] and "VENTA" in sent[-1]
    br.fill(ex["id"], close * 1.05, at="2024-06-05T13:30:00+00:00")
    tr.tick(pd.Timestamp("2024-06-05 15:00", tz="UTC"))
    assert "vendidas" in sent[-1] and "Resultado de la operación" in sent[-1]
    led = tr.ledger(tr.lab.get_bot(bot.id))
    assert led["qty"] == 0 and led["trades"][0]["pnl"] == pytest.approx(entry["qty"] * close * 0.04)
    curve = tr.live_curve(tr.lab.get_bot(bot.id), world["data"]["bars"])
    assert list(curve.index.strftime("%Y-%m-%d")) == ["2024-06-03", "2024-06-04"]
    assert curve.iloc[0] == pytest.approx(20_000)  # activated after the June 3 close, still flat
    assert curve.iloc[1] == pytest.approx(20_000 + entry["qty"] * (close * 1.05 - close * 1.01))  # long at the close


def test_entries_are_never_sent_late_but_exits_are(world):
    tr, br, sent, bot = world["trader"], world["broker"], world["sent"], world["bot"]
    tr.activate(bot.id, 10, follow_open=False)
    set_plan(world, {"2024-06-03": {"entry": 1}})
    br.is_open = True  # the app was opened after the next open
    tr.tick(NEXT_DAY_OPEN)
    assert not br.orders and "no se envía" in sent[-1]


def test_stops_and_targets_rest_at_alpaca(world):
    tr, br, bot = world["trader"], world["broker"], world["bot"]
    tr.activate(bot.id, 10, follow_open=False)
    close = float(world["data"]["bars"]["close"].iloc[-1])
    set_plan(world, {"2024-06-03": {"entry": 1, "stop_pct": 0.05, "target_pct": 0.10}})
    tr.tick(AFTER_CLOSE)
    e = br.last(side="buy")
    assert e["order_class"] == "bracket" and e["qty"] == int(e["qty"])  # resting stop / target: whole shares
    assert e["req"]["stop_loss"] == pytest.approx(close * 0.95) and e["req"]["take_profit"] == pytest.approx(close * 1.10)
    br.fill(e["id"], close)
    for leg in e["legs"]:  # the bracket's exit legs lapse at the end of the day
        br.orders[leg["id"]]["status"] = "expired"
    br.reject_gtc = True  # and if Alpaca refuses GTC protective orders, they are sent one day at a time
    add_day(world, "2024-06-04", close * 1.02)
    tr.tick(pd.Timestamp("2024-06-04 21:30", tz="UTC"))
    oco = br.last(order_class="oco")
    assert oco["time_in_force"] == "day" and oco["req"]["stop_loss"] == pytest.approx(close * 0.95)
    # the stop is hit at Alpaca while the computer is off: the fill is read back and reported
    br.fill(oco["legs"][0]["id"], close * 0.95, at="2024-06-05T15:00:00+00:00")
    tr.tick(pd.Timestamp("2024-06-05 16:00", tz="UTC"))
    assert "Stop ejecutado" in world["sent"][-1] and tr.ledger(tr.lab.get_bot(bot.id))["qty"] == 0


def test_safety_checks(world, sf):
    tr, br, sent, bot, lab = world["trader"], world["broker"], world["sent"], world["bot"], world["lab"]
    tr.activate(bot.id, 60, follow_open=False)
    dup = lab.create_bot("connors_rsi2", "SPY")
    with pytest.raises(ValueError, match="misma acción"):
        tr.activate(dup.id, 10)
    second = lab.create_bot("connors_rsi2", "QQQ")
    with pytest.raises(ValueError, match="100%"):
        tr.activate(second.id, 50)
    # Alpaca's position differs from what the bot expects (manual trade): nothing is sent for that bot
    br.pos["SPY"] = 7
    set_plan(world, {"2024-06-03": {"entry": 1}})
    tr.tick(AFTER_CLOSE)
    assert not br.orders and "Descuadre" in sent[-1]
    br.pos.clear()
    # shorts only where Alpaca allows them
    tr._update_bot(bot.id, last_signal_day=None)
    set_plan(world, {"2024-06-03": {"entry": -1}})
    br.shortable = False
    tr.tick(AFTER_CLOSE)
    assert not br.orders and "corto" in sent[-1]
    # stopping the bot closes its position at the next open
    tr._update_bot(bot.id, last_signal_day=None)
    br.shortable = True
    set_plan(world, {"2024-06-03": {"entry": 1}})
    tr.tick(AFTER_CLOSE)
    br.fill(br.last(side="buy")["id"], 100.0)
    tr.sync_orders(br)
    tr.deactivate(bot.id)
    assert br.last(side="sell")["qty"] == br.last(side="buy")["qty"] and lab.get_bot(bot.id).paper_status == "stopped"


def test_missing_prices_are_downloaded_first(world):
    tr, br, bot = world["trader"], world["broker"], world["bot"]
    tr.activate(bot.id, 10, follow_open=False)
    later = pd.Timestamp("2024-06-04 21:30", tz="UTC")  # the June 4 bar is not stored yet
    assert "descargando" in tr.tick(later) and world["refreshed"] == [["SPY"]]
    assert "descargando" in tr.tick(later) or "esperando" in tr.tick(later)
    assert not br.orders


def test_follow_the_backtest_position_on_activation(world):
    tr, br, bot = world["trader"], world["broker"], world["bot"]
    Toggle.plan = {"2024-05-01": {"entry": 1}}  # the backtest is long since May
    tr.activate(bot.id, 10, follow_open=True)
    assert tr.book(tr.lab.get_bot(bot.id))["SPY"]["pending"]["direction"] == 1
    tr.tick(AFTER_CLOSE)
    assert br.last(side="buy")["qty"] > 0 and not tr.book(tr.lab.get_bot(bot.id)).get("SPY", {}).get("pending")


class Multi(Strategy):
    """Test strategy for portfolio bots: per-stock signals by date ({symbol: {day: {column: value}}})."""
    key, name = "test_multi", "Escáner de prueba"
    default_symbols = ()
    universe = False
    plan: dict = {}
    ranks: dict = {}

    def signals(self, bars, p):
        out = pd.DataFrame({"entry": 0, "exit": False}, index=bars.index)
        for day, row in self.plan.get(bars.attrs.get("symbol"), {}).items():
            t = pd.Timestamp(day, tz="UTC")
            if t in out.index:
                for k, v in row.items():
                    out.loc[t, k] = v
        return out

    def rank(self, bars, p):
        return pd.Series(self.ranks.get(bars.attrs.get("symbol"), 0.0), index=bars.index, dtype=float)


@pytest.fixture
def uworld(sf, tmp_path):
    REGISTRY["test_multi"] = Multi()
    REGISTRY["test_toggle"] = Toggle()
    Multi.plan, Multi.ranks = {}, {"AAA": 2.0, "BBB": 1.0, "CCC": 3.0}
    data = {"v": 1, "frames": {}}
    for i, s in enumerate(["AAA", "BBB", "CCC"]):
        df = to_canonical(synthetic_daily("2023-01-03", "2024-06-03", seed=20 + i), Timeframe.D1)
        df.attrs["symbol"] = s
        data["frames"][s] = df

    def frame(s):
        if s not in data["frames"]:
            raise KeyError(s)
        return data["frames"][s]

    def universe():
        f = data["frames"]
        return {"symbols": {s: None for s in f}, "dated": True, "first": {s: f[s].index[0] for s in f},
                "last": {s: f[s].index[-1] for s in f}}
    lab = LabService(sf, frame, lambda s: (data["v"],), universe=universe, data_version=lambda: (data["v"],))
    broker, sent = FakeAlpaca(), []

    class Tg:
        def send(self, text):
            sent.append(text)
    trader = PaperTrader(sf, lab, lambda: broker, lambda: Tg(), refresh=lambda syms: True)
    bot = lab.create_bot("test_multi", "SP500", max_positions=2)
    yield {"lab": lab, "trader": trader, "broker": broker, "sent": sent, "bot": bot, "data": data}
    REGISTRY.pop("test_multi", None)
    REGISTRY.pop("test_toggle", None)


def uplan(w, plan):
    Multi.plan = plan
    w["data"]["v"] += 1


def uadd_day(w, day):
    t = pd.Timestamp(day, tz="UTC")
    for s, bars in list(w["data"]["frames"].items()):
        c = float(bars["close"].iloc[-1])
        row = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6},
                           index=pd.DatetimeIndex([t], name="ts"))
        df = pd.concat([bars, to_canonical(row, Timeframe.D1)])
        df.attrs["symbol"] = s
        w["data"]["frames"][s] = df
    w["data"]["v"] += 1


def test_portfolio_bot_buys_the_best_ranked_stocks_up_to_its_places(uworld):
    tr, br, sent, bot, lab = uworld["trader"], uworld["broker"], uworld["sent"], uworld["bot"], uworld["lab"]
    tr.activate(bot.id, 50, follow_open=False, now=AFTER_CLOSE)
    uplan(uworld, {s: {"2024-06-03": {"entry": 1}} for s in ("AAA", "BBB", "CCC")})
    assert "calculando" in tr.tick(AFTER_CLOSE) and not br.orders  # the scanner is recomputed with the new close
    lab.wait()
    tr.tick(AFTER_CLOSE)
    buys = [o for o in br.orders.values() if o["side"] == "buy"]
    assert sorted(o["symbol"] for o in buys) == ["AAA", "CCC"]  # 2 places: ranks 3 (CCC) and 2 (AAA)
    for o in buys:  # each place gets half of the bot's 50,000
        close = float(uworld["data"]["frames"][o["symbol"]]["close"].iloc[-1])
        assert o["qty"] == pytest.approx(25_000 / close, abs=1e-4)
    assert "COMPRA" in sent[-1] and "S&P 500" in sent[-1]
    for o in buys:
        br.fill(o["id"], float(uworld["data"]["frames"][o["symbol"]]["close"].iloc[-1]))
    tr.tick(NEXT_DAY_OPEN)
    assert sum("Ejecutada" in x for x in sent) == 2
    assert sorted(tr.summary(lab.get_bot(bot.id))["positions"]) == ["AAA", "CCC"]
    # a stock bot cannot be activated on a stock the portfolio bot holds
    single = lab.create_bot("test_toggle", "CCC")
    with pytest.raises(ValueError, match="misma acción"):
        tr.activate(single.id, 10)
    # next close: AAA exits, BBB signals again -> AAA sold and BBB bought in the freed place
    uadd_day(uworld, "2024-06-04")
    uplan(uworld, {"AAA": {"2024-06-03": {"entry": 1}, "2024-06-04": {"exit": True}},
                   "BBB": {"2024-06-03": {"entry": 1}, "2024-06-04": {"entry": 1}},
                   "CCC": {"2024-06-03": {"entry": 1}}})
    br.is_open = False
    later = pd.Timestamp("2024-06-04 21:30", tz="UTC")
    tr.tick(later)
    lab.wait()
    tr.tick(later)
    assert br.last(side="sell")["symbol"] == "AAA" and br.last(side="buy")["symbol"] == "BBB"
    d = lab.detail(bot.id)
    assert d["bot"]["kind"] == "universe" and d["config"]["max_positions"] == 2
    curve = tr.live_curve(lab.get_bot(bot.id), uworld["data"]["frames"]["AAA"], lambda s: uworld["data"]["frames"][s]["close"])
    assert len(curve) == 2 and curve.iloc[0] == pytest.approx(50_000)
    out = tr.deactivate(bot.id)
    assert sorted(out["symbols"]) == ["AAA", "CCC"]


class DayTrade(Strategy):
    key, name = "test_day", "Intradía de prueba"
    default_symbols = ()
    universe = False
    day_trade = True
    plan: dict = {}

    def signals(self, bars, p):
        out = pd.DataFrame({"entry": 0}, index=bars.index)
        for day, row in self.plan.items():
            t = pd.Timestamp(day, tz="UTC")
            if t in out.index:
                for k, v in row.items():
                    if k not in out:
                        out[k] = np.nan
                    out.loc[t, k] = v
        return out


def test_day_trades_close_at_the_close_and_limit_on_open_waits_for_its_window(world):
    tr, br, sent, lab = world["trader"], world["broker"], world["sent"], world["lab"]
    REGISTRY["test_day"] = DayTrade()
    try:
        bot = lab.create_bot("test_day", "QQQ")
        tr.activate(bot.id, 10, follow_open=False, now=AFTER_CLOSE)
        close = float(world["data"]["bars"]["close"].iloc[-1])
        DayTrade.plan = {"2024-06-03": {"entry": 1, "entry_limit": close * 0.99}}
        world["data"]["v"] += 1
        tr.tick(AFTER_CLOSE)  # 17:30 New York: Alpaca refuses opening-auction orders until 19:00
        assert not br.orders and "19:00" in sent[-1]
        tr.tick(pd.Timestamp("2024-06-03 23:30", tz="UTC"))  # 19:30 New York: sent, limit-on-open
        o = br.last(side="buy")
        assert o["type"] == "limit" and o["time_in_force"] == "opg" and o["req"]["limit_price"] == pytest.approx(close * 0.99)
        br.fill(o["id"], close * 0.985)
        br.is_open = True
        tr.tick(pd.Timestamp("2024-06-04 15:00", tz="UTC"))  # 11:00 New York: holding, nothing to do yet
        assert not [x for x in br.orders.values() if x["side"] == "sell"]
        tr.tick(pd.Timestamp("2024-06-04 19:45", tz="UTC"))  # 15:45 New York: market-on-close sell
        s = br.last(side="sell")
        assert s["time_in_force"] == "cls" and s["qty"] == o["qty"] and "cierre del día" in sent[-1]
        br.fill(s["id"], close, at="2024-06-04T20:00:00+00:00")
        br.is_open = False
        tr.tick(pd.Timestamp("2024-06-04 20:10", tz="UTC"))
        assert tr.ledger(lab.get_bot(bot.id), "QQQ")["qty"] == 0 and "Resultado" in sent[-1]
        # a missed close: the position is closed at the next open and reported
        o2 = tr._submit(br, lab.get_bot(bot.id), "QQQ", "buy", 5, "entry", None)
        br.fill(o2.broker_id, close)
        tr.tick(pd.Timestamp("2024-06-04 23:00", tz="UTC"))
        assert br.last(side="sell")["time_in_force"] == "day" and "no se cerró" in " ".join(sent[-3:])
    finally:
        REGISTRY.pop("test_day", None)


def test_recheck_sends_what_was_skipped_without_repeating(uworld):
    tr, br, lab, bot = uworld["trader"], uworld["broker"], uworld["lab"], uworld["bot"]
    br.equity, br.fractionable = 40.0, False  # 20 $ per place: not enough for one whole share
    tr.activate(bot.id, 100, follow_open=False, now=AFTER_CLOSE)
    uplan(uworld, {s: {"2024-06-03": {"entry": 1}} for s in ("AAA", "BBB", "CCC")})
    tr.tick(AFTER_CLOSE)
    lab.wait()
    tr.tick(AFTER_CLOSE)
    assert not br.orders and "no llega" in uworld["sent"][-1]
    br.fractionable = True  # the fix: fractions of a share
    tr.recheck(bot.id, AFTER_CLOSE)
    buys = [o for o in br.orders.values() if o["side"] == "buy"]
    assert sorted(o["symbol"] for o in buys) == ["AAA", "CCC"] and all(0 < o["qty"] < 1 for o in buys)
    tr.recheck(bot.id, AFTER_CLOSE)  # again: nothing is sent twice
    assert len([o for o in br.orders.values() if o["side"] == "buy"]) == 2
    br.is_open = True
    with pytest.raises(ValueError, match="ya ha abierto"):
        tr.recheck(bot.id, NEXT_DAY_OPEN)
