import pytest

from qsts.config import ConfigurationError, Environment, Settings


def test_live_disabled_by_default(monkeypatch):
    for k in ("QSTS_ENV", "QSTS_LIVE_TRADING_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.env is Environment.DEVELOPMENT and not s.live_allowed_by_config


def test_live_flag_rejected_outside_live():
    with pytest.raises(ConfigurationError):
        Settings(_env_file=None, env="paper", live_trading_enabled=True)


def test_key_isolation():
    s = Settings(_env_file=None, env="paper", paper_broker_api_key="P", live_broker_api_key="L")
    assert s.broker_api_key() == "P"
    s = Settings(_env_file=None, env="live", paper_broker_api_key="P", live_broker_api_key="L")
    assert s.broker_api_key() == "L"
    assert Settings(_env_file=None, env="development", live_broker_api_key="L").broker_api_key() is None


def test_identical_keys_rejected():
    with pytest.raises(ConfigurationError):
        Settings(_env_file=None, env="paper", paper_broker_api_key="X", live_broker_api_key="X")


def test_secrets_not_in_repr():
    s = Settings(_env_file=None, env="paper", paper_broker_api_key="SUPERSECRET")
    assert "SUPERSECRET" not in repr(s)
