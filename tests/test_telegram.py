"""Telegram client and simulation messages. No network: the Bot API is replaced by a fake HTTP function."""
import pytest

from qsts.app.envfile import set_env_values
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
