"""Credentials, loaded only from environment variables (or a local ``.env`` file).

Secrets never live in YAML: the loader rejects secret-looking keys in config
files. In AWS the same variables are injected from Secrets Manager by the ECS
task definition, so application code is identical in every environment.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from adaptive_quant.core.errors import MissingSecretError


class Secrets(BaseSettings):
    """All credentials the platform may use. Every field is optional at load time;
    call :meth:`require` for the ones a given command actually needs."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    alpaca_api_key_id: SecretStr | None = Field(default=None, alias="ALPACA_API_KEY_ID")
    alpaca_api_secret_key: SecretStr | None = Field(default=None, alias="ALPACA_API_SECRET_KEY")
    polygon_api_key: SecretStr | None = Field(default=None, alias="POLYGON_API_KEY")
    database_url: SecretStr | None = Field(default=None, alias="DATABASE_URL")
    smtp_username: SecretStr | None = Field(default=None, alias="SMTP_USERNAME")
    smtp_password: SecretStr | None = Field(default=None, alias="SMTP_PASSWORD")
    live_trading_confirm: SecretStr | None = Field(default=None, alias="AQ_LIVE_TRADING_CONFIRM")

    def require(self, *fields: str) -> None:
        """Raise a readable error listing every missing variable at once."""
        missing = []
        for name in fields:
            info = type(self).model_fields[name]
            value = getattr(self, name)
            if value is None or not value.get_secret_value().strip():
                missing.append(info.alias or name.upper())
        if missing:
            raise MissingSecretError(
                f"missing required environment variable(s): {', '.join(missing)}",
                hint="copy .env.example to .env and fill them in, or export them in your shell",
            )

    def present(self) -> dict[str, bool]:
        """Which secrets are set (never their values) - safe to log or display."""
        return {
            (info.alias or name): getattr(self, name) is not None
            for name, info in type(self).model_fields.items()
        }


def load_secrets(env_file: Path | None = None) -> Secrets:
    """Load secrets from the process environment, optionally overlaid by ``env_file``.

    Real environment variables take precedence over the file.
    """
    if env_file is not None and env_file.exists():
        return Secrets(_env_file=env_file)
    return Secrets()
