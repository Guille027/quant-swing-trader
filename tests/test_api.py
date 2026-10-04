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
    assert st["last_options"] == {"use_ai": False, "avoid_earnings": True}  # the ranking shown after a restart
    lb = c.get("/api/autoresearch/leaderboard").json()
    assert lb["n_trials"] > 0 and lb["rows"] and lb["final_tests_used"] == 0
    assert c.post("/api/autoresearch/nope/final-test").status_code == 404
    first = lb["rows"][0]
    if first["status"] != "VALIDATED_PASS":
        assert c.post(f"/api/autoresearch/{first['id']}/final-test").status_code == 400


def test_data_manager_and_backtest_view(client, tmp_path):
    """Data jobs with a CSV provider (SYNTHETIC data), S&P list injection, PIT truncation, backtest view."""
    import time
    import pandas as pd
    from datetime import date
    from qsts.data.providers.csv_provider import CSVProvider
    from qsts.data.universe import UniverseList
    c, ctx = client
    root = tmp_path / "csv2"
    (root / "1d").mkdir(parents=True)
    for i, s in enumerate(["CCC", "DDD"]):
        df = synthetic_daily("2018-01-01", "2022-12-30", seed=20 + i)
        df.index.name = "ts"
        df.to_csv(root / "1d" / f"{s}.csv")
    ctx.extra["data_provider_factory"] = lambda: CSVProvider(root)
    members = pd.DataFrame({"symbol": ["CCC", "DDD", "EEE"], "name": ["C Co", "D Co", "E Co"],
                            "sector": ["Energy", "Utilities", "Energy"],
                            "date_added": pd.to_datetime(["2020-06-01", None, "2015-01-01"])})
    ctx.extra["sp500_fetcher"] = lambda: UniverseList("SP500", "test", "2026-01-01T00:00:00+00:00", members)

    def wait():
        for _ in range(300):
            j = c.get("/api/data/job").json()
            if not j["running"]:
                return j
            time.sleep(0.1)
        raise AssertionError("job did not finish")
    assert c.get("/api/data/sp500").json()["count"] == 3
    assert c.post("/api/data/ingest", json={"mode": "sp500"}).json()["started"]
    j = wait()
    assert j["ok"] == 2 and set(j["failed"]) == {"EEE"}  # no file for EEE: reported, not invented
    summ = c.get("/api/data/summary").json()
    by = {r["symbol"]: r for r in summ["symbols"]}
    assert summ["count"] == 5 and by["CCC"]["sector"] == "Energy" and by["CCC"]["sp500_since"] == "2020-06-01"
    assert c.post("/api/data/ingest", json={"mode": "update"}).json()["started"]
    assert wait()["up_to_date"] >= 0
    assert c.post("/api/data/ingest", json={"mode": "symbols", "symbols": []}).status_code == 400
    # research ignores CCC's history before it joined the index
    r = ctx.autoresearcher()
    assert r.full["CCC"].index[0] >= pd.Timestamp("2020-06-01", tz="UTC")
    assert r.full["DDD"].index[0] < pd.Timestamp("2018-01-10", tz="UTC")
    r.seed_baselines()
    lb = r.leaderboard()
    bt = c.get(f"/api/autoresearch/{lb['rows'][0]['id']}/backtest").json()
    assert bt["includes_oos"] is False and bt["equity"] and "benchmark" in bt and bt["yearly"]
    assert bt["period"][1] < "2023-01-01"
    assert c.get("/api/autoresearch/nope/backtest").status_code == 404


def test_paper_endpoints(client):
    from qsts.strategy.lifecycle import Status
    from qsts.strategy.definition import definition_from_dict
    c, ctx = client
    v = c.get("/api/paper").json()
    assert v["active"] is False and v["candidates"] == []
    assert c.post("/api/paper/start", json={"strategy_id": "nope"}).status_code == 400
    sd = definition_from_dict(DEF)
    ctx.registry.register("p1", sd)
    rec = ctx.tracker.run_backtest(sd, {s: ctx.research_frame(s) for s in ("AAA", "BBB")}, __import__(
        "qsts.backtest.engine", fromlist=["x"]).BacktestConfig(), strategy_id="p1")
    ctx.registry.transition("p1", Status.BACKTESTED, reason="t", actor="system", evidence={"backtest_experiment_id": rec.id})
    ctx.registry.transition("p1", Status.VALIDATING, reason="t", actor="system")
    ctx.registry.transition("p1", Status.CANDIDATE, reason="t", actor="user",
                            evidence={"walk_forward_experiment_id": "x", "robustness_passed": True,
                                      "oos_experiment_id": "y", "monte_carlo_experiment_id": "z"})
    assert [x["strategy_id"] for x in c.get("/api/paper").json()["candidates"]] == ["p1"]
    r = c.post("/api/paper/start", json={"strategy_id": "p1", "capital": 5000})
    assert r.status_code == 400 and "actualízalos" in r.json()["detail"]  # test data ends in 2022: stale
    assert c.post("/api/paper/stop", json={}).status_code == 400


def test_earnings_download_columns_and_reproduce(client, tmp_path):
    import time
    import pandas as pd
    from qsts.data.providers.csv_provider import CSVProvider
    c, ctx = client
    r0 = c.post("/api/lab/backtest", json={"definition": DEF, "symbols": ["AAA"]}).json()  # recorded before earnings
    root = tmp_path / "csv3"
    (root / "earnings").mkdir(parents=True)
    pd.DataFrame({"announced_at": ["2020-01-30T21:00:00Z", "2020-04-30T12:00:00Z", "2020-07-30T21:00:00Z"],
                  "time_known": [True, True, True], "eps_estimate": [1.0, 1.0, 1.0], "eps_reported": [1.1, 0.9, 1.2],
                  "surprise_pct": [10.0, -10.0, 20.0]}).to_csv(root / "earnings" / "AAA.csv", index=False)
    ctx.extra["data_provider_factory"] = lambda: CSVProvider(root)
    assert c.post("/api/data/ingest", json={"mode": "earnings"}).json()["started"]
    for _ in range(300):
        j = c.get("/api/data/job").json()
        if not j["running"]:
            break
        time.sleep(0.1)
    assert j["earnings_ok"] == 1 and j["earnings_missing"] == 2  # BBB and SPY have none: reported, not invented
    summ = c.get("/api/data/summary").json()
    assert summ["earnings"]["symbols"] == 1 and summ["earnings_rule"]["blackout_days"] == 3
    f = ctx.research_frame("AAA")
    assert {"earn_days_since", "earn_surprise", "earn_days_to"} <= set(f.columns)
    assert f.loc["2020-08-03", "earn_surprise"].item() == 20.0
    assert "earn_days_to" not in ctx.research_frame("BBB")
    rp = c.post(f"/api/experiments/{r0['experiment_id']}/reproduce").json()
    assert rp["reproduced"] is True  # the experiment saw the data without earnings columns
    r = ctx.autoresearcher()
    assert r.bt.earnings_blackout_days == 3 and r.bt.exit_before_earnings is True
    from qsts.research.autoresearch import AutoResearchConfig
    r_off = ctx.autoresearcher(AutoResearchConfig(oos_start=ctx.settings.oos_start, avoid_earnings=False))
    assert r_off.bt.earnings_blackout_days == 0 and r_off.universe_id != r.universe_id  # separate rankings


def test_version_shutdown_and_no_cache(client):
    c, ctx = client
    assert c.get("/api/status").json()["code_version"]
    assert c.get("/").headers["cache-control"] == "no-store"
    assert c.get("/static/app.js").headers["cache-control"] == "no-store"
    assert c.post("/api/shutdown").status_code == 409  # not started by the launcher
    hit = []
    ctx.extra["shutdown"] = lambda: hit.append(1)
    assert c.post("/api/shutdown").json()["stopping"] and hit == [1]
