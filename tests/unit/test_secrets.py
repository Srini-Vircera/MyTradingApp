from pathlib import Path

import pytest

from adaptive_quant.config.secrets import Secrets, load_secrets
from adaptive_quant.core.errors import MissingSecretError


def test_require_lists_all_missing() -> None:
    with pytest.raises(MissingSecretError) as info:
        Secrets().require("alpaca_api_key_id", "alpaca_api_secret_key")
    assert "ALPACA_API_KEY_ID" in str(info.value)
    assert "ALPACA_API_SECRET_KEY" in str(info.value)
    assert ".env" in str(info.value)


def test_blank_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "   ")
    with pytest.raises(MissingSecretError):
        Secrets().require("alpaca_api_key_id")


def test_reads_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PK123")
    s = Secrets()
    s.require("alpaca_api_key_id")
    assert s.alpaca_api_key_id is not None
    assert s.alpaca_api_key_id.get_secret_value() == "PK123"


def test_env_file_and_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POLYGON_API_KEY=from-file\nDATABASE_URL=postgresql://x\n")
    monkeypatch.setenv("POLYGON_API_KEY", "from-env")
    s = load_secrets(env_file)
    assert s.polygon_api_key is not None and s.polygon_api_key.get_secret_value() == "from-env"
    assert s.database_url is not None


def test_missing_env_file_is_fine(tmp_path: Path) -> None:
    assert load_secrets(tmp_path / "absent.env").database_url is None


def test_present_never_exposes_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    present = Secrets().present()
    assert present["SMTP_PASSWORD"] is True
    assert present["DATABASE_URL"] is False
    assert "pw" not in str(present)
