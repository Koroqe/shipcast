"""Logging configuration for the shipcast CLI.

Sets up:

* A `RichHandler` for console output (no locals, no Rich tracebacks — see
  `.claude/rules/security.md` BLOCKER-1 on traceback locals).
* A per-project JSON-line file handler under `<project>/logs/<YYYYMMDDTHHMMSSZ>.log`
  when a project context is active. Each log record is exactly one line of
  JSON; multi-line tracebacks are encoded as a single string field with
  embedded `\\n`, NEVER split across multiple records.

Defense-in-depth: a `SecretRedactionFilter` scans every emitted record for
known API-key patterns (Anthropic, OpenAI, Replicate, ElevenLabs, Google
AI Studio / Gemini) and redacts matching substrings to `[REDACTED]`. The
Replicate pattern is retained even though Replicate is no longer a
runtime dependency — historical log files may still contain those keys.

`configure()` is idempotent — re-invoking it without `reset_for_testing()`
returns immediately without altering handlers.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from rich.logging import RichHandler

#: Logger name used by all shipcast code. CLI/dispatcher logs go here.
LOGGER_NAME: Final[str] = "shipcast"

#: Substring patterns that match known API-key formats. Matches are redacted
#: in every log record before persistence (belt-and-braces in addition to
#: SecretStr masking at the Settings boundary).
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]+"),
    re.compile(r"r8_[A-Za-z0-9]+"),
    re.compile(r"xi-[A-Za-z0-9_-]{20,}"),
    # Google AI Studio / Gemini API keys (standard `AIzaSy...` prefix).
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
)
_REDACTED: Final[str] = "[REDACTED]"

#: Attributes that Python's `logging` framework already sets on a LogRecord.
#: Anything outside this set was supplied via `logger.X(msg, extra={...})` and
#: should be surfaced as a structured field in the JSON-line payload.
_STANDARD_LOG_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "extra_fields", "taskName",
        "asctime",
    }
)

_configured: bool = False
_file_handler: logging.Handler | None = None


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


class SecretRedactionFilter(logging.Filter):
    """Strip likely API-key values from every log record (defense in depth)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = _redact(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


class JsonLineFormatter(logging.Formatter):
    """Format each log record as a single line of JSON.

    Tracebacks are produced via `traceback.format_exception` (no
    `capture_locals=True`) so frame locals — which can contain plain-text
    secrets after `SecretStr.get_secret_value()` — are NEVER persisted.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        # Include any structured extras passed via `logger.X(msg, extra={...})`.
        # Python's logging sets each `extra` key directly on `record.__dict__`,
        # so we copy anything not in the standard LogRecord attribute set.
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_ATTRS or key in payload or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            # format_exception captures filename / lineno / source-line only —
            # NOT frame locals. See module docstring + security review BLOCKER-1.
            tb_lines = traceback.format_exception(*record.exc_info)
            payload["traceback"] = _redact("".join(tb_lines))

        return json.dumps(payload, ensure_ascii=False)


def configure(project_path: Path | None = None) -> Path | None:
    """Initialize logging. Idempotent.

    Args:
        project_path: when supplied, a JSON-line file handler is added that
            writes to `<project_path>/logs/<YYYYMMDDTHHMMSSZ>.log`. When `None`,
            only the console handler is configured (used by commands that run
            before a project exists, e.g. `shipcast --version`).

    Returns:
        The absolute path of the per-project log file, or `None` if no
        project context was supplied.
    """
    global _configured, _file_handler

    if _configured:
        # Idempotent: caller's responsibility to `reset_for_testing` if a
        # different context is needed.
        if _file_handler is not None and isinstance(_file_handler, logging.FileHandler):
            return Path(_file_handler.baseFilename)
        return None

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    # Clear any preexisting handlers (e.g., from pytest).
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.propagate = False

    console = RichHandler(
        rich_tracebacks=False,  # use stdlib traceback formatting (no locals)
        show_path=False,
        show_time=True,
        show_level=True,
    )
    console.addFilter(SecretRedactionFilter())
    logger.addHandler(console)

    log_file: Path | None = None
    if project_path is not None:
        logs_dir = project_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        log_file = logs_dir / f"{ts}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonLineFormatter())
        file_handler.addFilter(SecretRedactionFilter())
        logger.addHandler(file_handler)
        try:
            log_file.chmod(0o600)
        except OSError:
            pass  # not critical; some filesystems don't honor chmod
        _file_handler = file_handler

    _configured = True
    return log_file


def reset_for_testing() -> None:
    """Tear down logging state. Tests use this between cases."""
    global _configured, _file_handler
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    _file_handler = None
    _configured = False
