import pytest

from qsts.config import ConfigurationError, Environment, Settings
from qsts.core.kill_switch import KillSwitch
from qsts.core.modes import ModeController, ModeTransitionError, SystemMode


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


def test_mode_progression():
    mc = ModeController()
    with pytest.raises(ModeTransitionError):
        mc.transition(SystemMode.PAPER, reason="skip")
    mc.transition(SystemMode.BACKTEST, reason="ok")
    mc.transition(SystemMode.PAPER, reason="ok")
    with pytest.raises(ModeTransitionError):
        mc.transition(SystemMode.MANUAL_APPROVAL, reason="no confirm")
    mc.transition(SystemMode.MANUAL_APPROVAL, reason="ok", confirmed_by_user=True)
    mc.transition(SystemMode.OBSERVATION, reason="downgrade always allowed")
    assert mc.mode is SystemMode.OBSERVATION


def test_kill_switch(tmp_path):
    ks = KillSwitch(tmp_path)
    assert ks.trading_allowed()
    ks.engage("test")
    assert not ks.trading_allowed() and ks.info()["reason"] == "test"
    assert not KillSwitch(tmp_path).trading_allowed()  # persists across instances/processes
    with pytest.raises(PermissionError):
        ks.release(confirmed_by_user=False)
    ks.release(confirmed_by_user=True)
    assert ks.trading_allowed()


def test_kill_switch_fail_safe(tmp_path, monkeypatch):
    ks = KillSwitch(tmp_path)
    monkeypatch.setattr(type(ks.path), "exists", lambda self: (_ for _ in ()).throw(OSError("disk")))
    assert not ks.trading_allowed()
