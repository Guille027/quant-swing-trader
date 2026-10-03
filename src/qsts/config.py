"""Environment-scoped configuration.

Security rules enforced here:
- No secrets in code; everything comes from environment / .env.
- Each environment (development / paper / live) only ever exposes its own broker key.
  A LIVE key can never be read while running PAPER and vice versa.
- Live trading is disabled unless QSTS_ENV=live AND QSTS_LIVE_TRADING_ENABLED=true.
  Even then, the live safety system (phase 20) must still pass its checks.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEVELOPMENT = "development"
    PAPER = "paper"
    LIVE = "live"


class ConfigurationError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QSTS_", env_file=".env", extra="ignore")

    env: Environment = Environment.DEVELOPMENT
    database_url: str = "sqlite:///var/qsts.db"
    state_dir: Path = Path("var")
    live_trading_enabled: bool = False

    paper_broker_api_key: SecretStr | None = None
    live_broker_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # Default random seed for reproducible experiments.
    default_seed: int = Field(default=42)

    @model_validator(mode="after")
    def _check_environment_separation(self) -> "Settings":
        if self.live_trading_enabled and self.env is not Environment.LIVE:
            raise ConfigurationError(
                "QSTS_LIVE_TRADING_ENABLED=true is only valid with QSTS_ENV=live"
            )
        pk = self.paper_broker_api_key.get_secret_value() if self.paper_broker_api_key else ""
        lk = self.live_broker_api_key.get_secret_value() if self.live_broker_api_key else ""
        if pk and lk and pk == lk:
            raise ConfigurationError("Paper and live broker keys must differ")
        return self

    def broker_api_key(self) -> str | None:
        """Return ONLY the key belonging to the current environment."""
        if self.env is Environment.LIVE:
            key = self.live_broker_api_key
        elif self.env is Environment.PAPER:
            key = self.paper_broker_api_key
        else:
            return None  # development never talks to a broker
        return key.get_secret_value() if key else None

    @property
    def live_allowed_by_config(self) -> bool:
        return self.env is Environment.LIVE and self.live_trading_enabled


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)
