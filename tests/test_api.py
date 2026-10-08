"""The app's API end to end with SYNTHETIC prices (CSV provider) and a FAKE Alpaca (no network)."""
import time

import pytest
from fastapi.testclient import TestClient

from conftest import synthetic_daily
from test_trader import FakeAlpaca


@pytest.fixture
def app(tmp_path):
    from qsts.api.server import create_app
    from qsts.app.context import build_context
    from qsts.config import Settings
    from qsts.data.providers.csv_provider import CSVProvider
    ctx = build_context(Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/q.db", state_dir=tmp_path / "var"))
    root = tmp_path / "csv"
    (root / "1d").mkdir(parents=True)
    for i, s in enumerate(["SPY", "QQQ", "GLD", "AAPL"]):
        d = synthetic_daily("2012-01-01", "2024-12-31", seed=i)
        d.index.name = "ts"
        d.to_csv(root / "1d" / f"{s}.csv")
    broker = FakeAlpaca()
    ctx.extra.update(data_provider_factory=lambda: CSVProvider(root), trader_autostart=False,
                     env_path=str(tmp_path / ".env"))
    c = TestClient(create_app(ctx))
    return c, ctx, broker


def wait_job(c):
    for _ in range(300):
        if not c.get("/api/data/job").json()["running"]:
            return
        time.sleep(0.1)
    raise AssertionError("download did not finish")


def test_library_detail_and_audit(app):
    c, ctx, _ = app
    lib = c.get("/api/library").json()
    assert lib["missing"] == ["GLD", "QQQ", "SPY"]  # the default bots need prices: one click downloads them
    assert c.post("/api/data/ingest", json={"mode": "symbols", "symbols": lib["missing"]}).json()["started"]
    wait_job(c)
    lib = c.get("/api/library").json()
    assert not lib["missing"] and all("error" not in r for r in lib["rows"]) and lib["n_strategies"] >= 3
    r = c.post("/api/bots", json={"strategy": "golden_cross", "symbol": "aapl"}).json()
    assert r["id"] == "golden_cross-aapl" and r["downloading"] is True  # no prices yet: downloaded at once
    wait_job(c)
    d = c.get("/api/bots/golden_cross-aapl").json()
    assert d["bot"]["symbol"] == "AAPL" and d["equity"] and d["paper"] is None and d["summary"]["n_trades"] >= 1
    a = c.get("/api/bots/golden_cross-aapl/audit").json()
    assert a["checks"] and a["verdict"]
    assert c.post("/api/bots", json={"strategy": "nope", "symbol": "AAPL"}).status_code == 404
    assert c.post("/api/bots", json={"strategy": "golden_cross", "symbol": "AAPL", "params": {"x": 1}}).status_code == 400
    assert c.post("/api/bots/golden_cross-aapl/update", json={"favorite": True}).json() == {"ok": True}
    assert next(x for x in c.get("/api/library").json()["rows"] if x["id"] == "golden_cross-aapl")["favorite"]
    assert c.post("/api/bots/golden_cross-aapl/update", json={"size_pct": 150}).status_code == 400  # no leverage
    assert c.get("/api/bots/nope").status_code == 404


def test_alpaca_keys_paper_trading_and_stop_all(app, tmp_path):
    c, ctx, broker = app
    c.post("/api/data/ingest", json={"mode": "symbols", "symbols": ["SPY"]})
    wait_job(c)
    assert c.get("/api/alpaca").json() == {"configured": False}
    assert c.post("/api/bots/connors_rsi2-spy/activate", json={"allocation_pct": 20}).status_code == 400  # no keys yet
    assert c.post("/api/alpaca/keys", json={"key": "short", "secret": "x"}).status_code == 400
    ctx.extra["broker_check"] = lambda k, s: broker.account()  # Alpaca says the keys are valid
    r = c.post("/api/alpaca/keys", json={"key": "PKTESTKEY123456", "secret": "s" * 40})
    assert r.json()["saved"] and "QSTS_ALPACA_API_KEY=PKTESTKEY123456" in (tmp_path / ".env").read_text()
    st = c.get("/api/status").json()
    assert st["alpaca"] is True and "PKTESTKEY" not in str(st)  # the keys never go back to the screen
    ctx.extra["broker_factory"] = lambda: broker
    act = c.post("/api/bots/connors_rsi2-spy/activate", json={"allocation_pct": 20, "follow_open": False}).json()
    assert act["capital"] == pytest.approx(20_000)
    d = c.get("/api/bots/connors_rsi2-spy").json()
    assert d["bot"]["paper_status"] == "active" and d["paper"]["capital"] == pytest.approx(20_000)
    p = c.get("/api/paper").json()
    assert [b["id"] for b in p["bots"]] == ["connors_rsi2-spy"] and p["events"]
    assert c.post("/api/bots/connors_rsi2-qqq/activate", json={"allocation_pct": 90}).status_code in (400, 404)
    assert c.post("/api/paper/run").json()["state"]
    out = c.post("/api/stop-all", json={"close": True}).json()
    assert [x["bot"] for x in out["stopped"]] == ["connors_rsi2-spy"]
    assert c.get("/api/bots/connors_rsi2-spy").json()["bot"]["paper_status"] == "stopped"
    assert c.post("/api/bots/connors_rsi2-spy/deactivate", json={}).status_code == 400
