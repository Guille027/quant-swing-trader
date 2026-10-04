"""Telegram client and simulation messages. No network: the Bot API is replaced by a fake HTTP function."""
import pytest

from qsts.app.envfile import set_env_values
from qsts.notify.report import daily_messages, num
from qsts.notify.telegram import Telegram, TelegramError, split_message

TOKEN = "123456:ABC-fake"


def fake_http(responses, calls):
    def http(url, payload, timeout):
        calls.append((url, payload))
        return responses.pop(0) if responses else {"ok": True, "result": True}
    return http


def test_client_calls_bot_api_and_hides_the_token():
    calls = []
    tg = Telegram(TOKEN, 42, http=fake_http([], calls), pause=0)
    assert tg.send("<b>hola</b>") == 1
    url, payload = calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload == {"chat_id": 42, "text": "<b>hola</b>", "parse_mode": "HTML"}
    bad = Telegram(TOKEN, 42, http=fake_http([{"ok": False, "error_code": 400,
                                               "description": f"Bad Request: chat not found {TOKEN}"}], []))
    with pytest.raises(TelegramError) as e:
        bad.send("x")
    assert "chat not found" in str(e.value) and TOKEN not in str(e.value)
    boom = Telegram(TOKEN, 42, http=lambda *a: (_ for _ in ()).throw(OSError(f"proxy refused {TOKEN}")))
    with pytest.raises(TelegramError) as e:
        boom.send("x")
    assert TOKEN not in str(e.value)
    with pytest.raises(TelegramError):
        Telegram("not-a-token")
    with pytest.raises(TelegramError):  # no chat yet
        Telegram(TOKEN, http=fake_http([], [])).send("x")


def test_find_chat_and_long_messages():
    upd = {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 777, "first_name": "Ana"}, "text": "hola"}}]}
    assert Telegram(TOKEN, http=fake_http([upd], [])).find_chat() == {"id": 777, "name": "Ana"}
    assert Telegram(TOKEN, http=fake_http([{"ok": True, "result": []}], [])).find_chat() is None
    parts = split_message("\n".join(["x" * 100] * 100), limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts).count("x") == 10_000
    calls = []
    assert Telegram(TOKEN, 1, http=fake_http([], calls), pause=0).send("y\n" * 3000) == len(calls) > 1


VIEW = {"active": True, "currency": "EUR", "fx": 1.1257, "as_of": "2026-10-02", "next_open": "2026-10-05T13:30:00+00:00",
        "equity": 2401.2, "pnl": 38.2, "return": 0.0162, "stale_sessions": 0, "benchmark": {"return": 0.012},
        "orders": [{"action": "VENDER", "symbol": "MSFT", "qty": 0.512, "approx_value": 180.0, "reason": "signal_exit"},
                   {"action": "COMPRAR", "symbol": "NVDA", "qty": 0.553, "approx_value": 236.0, "last_close": 420.1,
                    "approx_stop": 400.0, "approx_target": 460.0, "likely": True, "partial": False, "earnings_in": None}],
        "closed": [{"symbol": "AAPL", "exit": "2026-10-02", "pnl": 12.3, "pnl_pct": 0.041, "exit_price": 250.0,
                    "reason": "target"}],
        "positions": [{"symbol": "MSFT", "pnl_pct": 0.032, "unrealized_pnl": 7.5, "bars_held": 4, "stop": 400.0,
                       "target": 470.0, "earnings_in": None}]}


def test_daily_messages_say_what_to_do_at_the_next_open():
    alert, summary = daily_messages(VIEW)
    assert "VENDER MSFT" in alert and "señal de salida" in alert and "lunes 5 oct, 15:30 h" in alert
    assert "AAPL +12,30 €" in alert and "objetivo de beneficio" in alert
    assert "cierre del viernes 2 oct" in summary and "2.401,20 €" in summary
    assert "COMPRAR NVDA" in summary and "236,00 €" in summary and "stop ≈ 400,00 $" in summary
    assert "MSFT +3,2 %" in summary and "1 € = 1,1257 $" in summary
    quiet = {**VIEW, "orders": [], "closed": []}
    msgs = daily_messages(quiet)
    assert len(msgs) == 1 and "Nada que comprar ni vender" in msgs[0]
    assert daily_messages({"active": False}) == [] and num(-1234.5) == "−1.234,50"


def test_env_file_keeps_other_settings(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nQSTS_GEMINI_API_KEY=abc\nQSTS_TELEGRAM_CHAT_ID=1\n")
    set_env_values({"QSTS_TELEGRAM_CHAT_ID": "777", "QSTS_TELEGRAM_BOT_TOKEN": TOKEN}, p)
    assert p.read_text().splitlines() == ["# comment", "QSTS_GEMINI_API_KEY=abc", "QSTS_TELEGRAM_CHAT_ID=777",
                                          f"QSTS_TELEGRAM_BOT_TOKEN={TOKEN}"]


def test_telegram_setup_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from qsts.api.server import create_app
    from qsts.app.context import build_context
    from qsts.config import Settings
    st = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/q.db", state_dir=tmp_path / "var")
    ctx = build_context(st)
    calls = []
    responses = {"getMe": {"ok": True, "result": {"username": "mi_qsts_bot", "first_name": "QSTS"}},
                 "getUpdates": {"ok": True, "result": [{"update_id": 5, "message": {"chat": {"id": 999, "first_name": "Ana"}}}]}}
    def http(url, payload, timeout):
        calls.append((url.rsplit("/", 1)[1], payload))
        return responses.get(url.rsplit("/", 1)[1], {"ok": True, "result": True})
    ctx.extra.update(env_path=str(tmp_path / ".env"),
                     telegram_factory=lambda tok, chat: Telegram(tok, chat, http=http, pause=0))
    c = TestClient(create_app(ctx))
    assert c.get("/api/telegram").json()["configured"] is False
    assert c.post("/api/telegram/test").status_code == 400
    assert c.post("/api/telegram/token", json={"token": TOKEN}).json()["bot"] == "mi_qsts_bot"
    assert c.post("/api/telegram/detect").json() == {"id": 999, "name": "Ana"}
    env = (tmp_path / ".env").read_text()
    assert f"QSTS_TELEGRAM_BOT_TOKEN={TOKEN}" in env and "QSTS_TELEGRAM_CHAT_ID=999" in env
    t = c.get("/api/telegram").json()
    assert t["configured"] is True and TOKEN not in str(t)  # the token never goes back to the screen
    assert c.post("/api/telegram/test").json() == {"sent": True} and calls[-1][1]["chat_id"] == "999"
    assert c.post("/api/telegram/report").status_code == 400  # no simulation running
