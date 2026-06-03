"""Slice 1 — package skeleton, version, .env.example, lazy-import smoke tests.

Owned TCs:
- TC-19.1: `.env.example` lists only bare key names with empty values.
- TC-23.3/TC-23.4 (import discipline): importing the package and its core
  modules does NOT eagerly pull heavy SDKs (elevenlabs, requests, torch,
  whisper) — they must stay lazy inside `stage.run()` / client methods.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

import shipcast
from shipcast.cli import app

# PEP 440 — the public version subset this project will use.
_PEP440 = re.compile(
    r"^\d+(\.\d+){0,2}"
    r"((a|b|rc)\d+)?"
    r"(\.post\d+)?"
    r"(\.dev\d+)?"
    r"(\+[A-Za-z0-9.]+)?$"
)

# Repo root resolved from tests/unit/test_package_imports.py
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_shipcast_exposes_version() -> None:
    """shipcast.__version__ exists and is a non-empty PEP-440-ish string."""
    assert hasattr(shipcast, "__version__")
    assert isinstance(shipcast.__version__, str)
    assert shipcast.__version__
    assert _PEP440.match(shipcast.__version__), shipcast.__version__


def test_cli_version_flag() -> None:
    """`shipcast --version` prints the version and exits 0."""
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert shipcast.__version__ in result.output


def test_tc_19_1_env_example_contains_no_real_values() -> None:
    """TC-19.1: `.env.example` lists key names with empty values only.

    No real secrets, no comments containing keys. Each non-blank line must be
    exactly `<KEY_NAME>=` (nothing after the `=`).
    """
    env_example = REPO_ROOT / ".env.example"
    assert env_example.is_file(), f"missing {env_example}"

    expected_keys = {"ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "GEMINI_API_KEY"}
    seen_keys: set[str] = set()

    for raw_line in env_example.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        assert not line.startswith("#"), f".env.example must not contain comments: {raw_line!r}"
        assert line.endswith("="), f".env.example line must end with '=' (empty value): {raw_line!r}"
        key = line[:-1]
        assert key.isidentifier(), f"invalid env key name: {key!r}"
        seen_keys.add(key)

    assert seen_keys == expected_keys, f"unexpected key set in .env.example: {seen_keys}"


# --------------------------------------------------------------------------- #
# TC-23.3/TC-23.4 — lazy-import discipline (subprocess, clean sys.modules)
# --------------------------------------------------------------------------- #

_HEAVY_PREFIXES = ("elevenlabs", "requests", "torch", "whisper")


def _assert_no_heavy_imports(import_stmt: str) -> None:
    """Import `import_stmt` in a fresh interpreter; assert no heavy SDK leaked."""
    probe = (
        f"{import_stmt}\n"
        "import sys\n"
        f"heavy = {_HEAVY_PREFIXES!r}\n"
        "leaked = sorted(\n"
        "    m for m in sys.modules\n"
        "    if any(m == h or m.startswith(h + '.') for h in heavy)\n"
        ")\n"
        "assert not leaked, f'heavy SDKs leaked: {leaked}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Subprocess exited {result.returncode}.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_tc_23_3_import_shipcast_package_is_lazy() -> None:
    """TC-23.3: `import shipcast` does not eagerly import heavy SDKs."""
    _assert_no_heavy_imports("import shipcast")


def test_tc_23_4_import_cli_is_lazy() -> None:
    """TC-23.4: `import shipcast.cli` does not eagerly import heavy SDKs."""
    _assert_no_heavy_imports("import shipcast.cli")


def test_import_core_modules_is_lazy() -> None:
    """Importing manifest/project/stages/clients does not pull heavy SDKs."""
    _assert_no_heavy_imports(
        "import shipcast.manifest, shipcast.project, shipcast.stages, shipcast.clients"
    )
