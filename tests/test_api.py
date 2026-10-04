import pytest
from fastapi.testclient import TestClient

from conftest import synthetic_daily
from qsts.app.context import build_context
from qsts.cli import main
from qsts.config import Settings
from qsts.api.server import create_app


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "csv"
    (root / "1d").mkdir(parents=True)
    for i, s in enumerate(["SPY", "AAA", "BBB"]):
        df = synthetic_daily("2018-01-01", "2022-12-30", seed=i)
        df.index.name = "ts"
        df.to_csv(root / "1d" / f"{s}.csv")
    st = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/q.db", state_dir=tmp_path / "var")
    ctx = build_context(st)
    import qsts.cli as cli
    cli.build_context = lambda: ctx
    main(["ingest", "--provider", "csv", "--root", str(root), "--symbols", "SPY,AAA,BBB", "--start", "2018-01-01",
          "--end", "2022-12-30"])
    return TestClient(create_app(ctx)), ctx


DEF = {"name": "t", "family": "trend", "hypothesis": "h", "direction": "long",
       "entry_long": [{"left": {"feature": "close"}, "op": ">", "right": {"feature": "sma", "params": {"n": 50}}}],
       "exit_long": [{"left": {"feature": "close"}, "op": "<", "right": {"feature": "sma", "params": {"n": 50}}}],
       "stop": {"kind": "atr", "atr_n": 14, "mult": 3.0}, "take_profit": {"kind": "none"}}


def test_status_and_ui(client):
    c, _ = client
    s = c.get("/api/status").json()
    assert s["mode"] == "OBSERVATION" and s["environment"] == "development" and not s["live_enabled_by_config"]
    assert s["symbols_with_data"] == 3
    assert "STOP ALL TRADING" in c.get("/").text
    assert c.get("/static/vendor/lightweight-charts.standalone.production.js").status_code == 200


def test_chart_and_asof(client):
    c, _ = client
    d = c.get("/api/chart/AAA?indicators=ema:20,rsi:14").json()
    assert len(d["candles"]) > 1000 and set(d["indicators"]) == {"ema(n=20)", "rsi(n=14)"}
    past = c.get("/api/chart/AAA?asof=2020-01-02T15:00:00Z").json()
    assert past["candles"][-1]["time"] < 1577923200 + 1  # 2020-01-02 bar not yet complete at 15:00 UTC
    assert c.get("/api/chart/NOPE").status_code == 404
    assert c.get("/api/chart/AAA?indicators=bogus").status_code == 400


def test_lab_backtest_and_reproduce(client):
    c, _ = client
    r = c.post("/api/lab/backtest", json={"definition": DEF, "symbols": ["AAA", "BBB"]}).json()
    assert r["metrics"]["n_trades"] > 0 and r["equity"]
    rp = c.post(f"/api/experiments/{r['experiment_id']}/reproduce").json()
    assert rp["reproduced"] is True
    assert any(e["id"] == r["experiment_id"] for e in c.get("/api/experiments").json())
    assert c.post("/api/lab/backtest", json={"definition": {**DEF, "entry_long": [{"left": {"feature": "zzz"}, "op": ">", "right": {"value": 1}}]},
                                             "symbols": ["AAA"]}).status_code == 400


def test_mode_kill_switch_and_scan(client):
    c, ctx = client
    assert c.post("/api/mode", json={"mode": "PAPER", "reason": "skip"}).status_code == 400
    assert c.post("/api/mode", json={"mode": "BACKTEST", "reason": "x"}).json()["mode"] == "BACKTEST"
    ctx.registry.register("t1", __import__("qsts.strategy.definition", fromlist=["x"]).definition_from_dict(DEF))
    sc = c.post("/api/scan", params={"asof": "2022-06-01T21:00:00Z", "universe": "AAA,BBB"}).json()
    assert sc["assets_scanned"] == 2 and sc["final_signals"] == 0  # RESEARCH strategies never produce signals
    assert "QUANT TRADING SYSTEM" in c.get("/api/scan/text").text
    assert c.post("/api/kill-switch", json={"engage": True, "reason": "test"}).json()["engaged"]
    assert c.post("/api/kill-switch", json={"engage": False}).status_code == 400
    assert not c.post("/api/kill-switch", json={"engage": False, "confirm": True}).json()["engaged"]


def test_split_adjusted_everywhere_and_point_in_time(tmp_path):
    """RAW bars with a real 4:1 split + the split action: research, charts and scans see continuous prices,
    and a replay before the ex-date does not use the (then unknown) split."""
    root = tmp_path / "csv"
    (root / "1d").mkdir(parents=True)
    (root / "actions").mkdir()
    for i, s in enumerate(["SPY", "AAA"]):
        df = synthetic_daily("2018-01-01", "2022-12-30", seed=i)
        if s == "AAA":
            df.loc[df.index >= "2021-06-01", ["open", "high", "low", "close"]] /= 4
            df.loc[df.index >= "2021-06-01", "volume"] *= 4
        df.index.name = "ts"
        df.to_csv(root / "1d" / f"{s}.csv")
    (root / "actions" / "AAA.csv").write_text("ex_date,kind,value\n2021-06-01,split,4.0\n")
    st = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/q.db", state_dir=tmp_path / "var")
    ctx = build_context(st)
    import qsts.cli as cli
    cli.build_context = lambda: ctx
    main(["ingest", "--provider", "csv", "--root", str(root), "--symbols", "SPY,AAA", "--start", "2018-01-01",
          "--end", "2022-12-30"])
    import numpy as np
    assert np.log(ctx.research_frame("AAA")["close"]).diff().abs().max() < 0.2
    assert np.log(ctx.adjusted_bars("AAA")["close"]).diff().abs().max() < 0.2
    before = ctx.adjusted_bars("AAA", asof="2021-05-01")  # split not yet known -> raw scale kept
    assert np.allclose(before["close"], ctx.load_bars("AAA")["close"])
    c = TestClient(create_app(ctx))
    closes = [x["close"] for x in c.get("/api/chart/AAA").json()["candles"]]
    assert np.abs(np.diff(np.log(closes))).max() < 0.2


def test_autoresearch_endpoints(client):
    import time
    c, ctx = client
    st = c.get("/api/autoresearch/status").json()
    assert st["running"] is False and "oos_start" in st
    assert c.post("/api/autoresearch/start", json={"use_ai": False, "max_cycles": 1, "population": 4,
                                                   "generations": 1}).json()["started"] is True
    for _ in range(600):
        st = c.get("/api/autoresearch/status").json()
        if not st["running"]:
            break
        time.sleep(0.2)
    assert st["running"] is False and st["error"] is None and st["cycles_done"] == 1, st
    lb = c.get("/api/autoresearch/leaderboard").json()
    assert lb["n_trials"] > 0 and lb["rows"] and lb["final_tests_used"] == 0
    assert c.post("/api/autoresearch/nope/final-test").status_code == 404
    first = lb["rows"][0]
    if first["status"] != "VALIDATED_PASS":
        assert c.post(f"/api/autoresearch/{first['id']}/final-test").status_code == 400
