import io
import json

from adaptive_quant.observability.logging import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)


def test_json_logs_are_structured_utc_and_redacted() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "json", stream=stream)
    bind_context(run_id="run-1", config_version="paper-abc")
    try:
        get_logger("test").info("order_submitted", symbol="TQQQ", api_key="PK-SECRET")
    finally:
        clear_context()
    record = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert record["event"] == "order_submitted"
    assert record["run_id"] == "run-1"
    assert record["config_version"] == "paper-abc"
    assert record["api_key"] == "***"
    assert record["timestamp"].endswith("Z")
    assert "PK-SECRET" not in stream.getvalue()


def test_level_filtering() -> None:
    stream = io.StringIO()
    configure_logging("WARNING", "json", stream=stream)
    get_logger("test").info("hidden")
    get_logger("test").warning("shown")
    output = stream.getvalue()
    assert "shown" in output and "hidden" not in output


def test_console_format() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "console", stream=stream)
    get_logger("test").info("hello", password="pw")
    assert "hello" in stream.getvalue()
    assert "pw" not in stream.getvalue().replace("password", "")
