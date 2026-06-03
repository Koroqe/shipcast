"""Logging setup tests.

Owned TC:
- TC-2.6: `configure(project)` creates a JSON-line log file under
  `<project>/logs/`; SecretStr values never appear in log output (redaction).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from shipcast.config import Settings
from shipcast.logging_setup import LOGGER_NAME, configure, reset_for_testing


@pytest.fixture(autouse=True)
def _reset_logging() -> object:
    """Tear logging down before and after each test (state is module-global)."""
    reset_for_testing()
    yield
    reset_for_testing()


def test_tc_2_6_configure_creates_jsonline_log_file(tmp_path: Path) -> None:
    """TC-2.6: configure(project) creates a JSON-line log file under <project>/logs/."""
    log_file = configure(tmp_path)
    assert log_file is not None
    assert log_file.parent == tmp_path / "logs"
    assert log_file.suffix == ".log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.info("hello world", extra={"event": "test_event"})
    for handler in logger.handlers:
        handler.flush()

    lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "expected at least one log line"
    record = json.loads(lines[-1])
    assert record["event"] == "test_event"
    assert "hello world" in json.dumps(record)


def test_configure_without_project_returns_none() -> None:
    """configure(None) sets up console-only logging and returns no file path."""
    assert configure(None) is None


def test_tc_2_6_secretstr_not_in_log_output(tmp_path: Path) -> None:
    """TC-2.6: a SecretStr's raw value never leaks into the log file.

    Pydantic's SecretStr.__repr__ masks the value; logging a Settings dump or a
    SecretStr object must not write the secret bytes.
    """
    log_file = configure(tmp_path)
    assert log_file is not None
    secret_value = "super-secret-key-12345"
    settings = Settings(elevenlabs_api_key=SecretStr(secret_value))

    logger = logging.getLogger(LOGGER_NAME)
    # Log the SecretStr object directly and a model dump (both must be masked).
    logger.info("settings: %s", settings.elevenlabs_api_key)
    logger.info("dump: %s", settings.model_dump(mode="python"))
    for handler in logger.handlers:
        handler.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert secret_value not in contents
