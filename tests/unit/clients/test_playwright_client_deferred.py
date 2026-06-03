"""Slice 8 — Playwright lazy-import discipline.

Importing ``shipcast.clients.playwright_client`` (or any transitive import from
``shipcast.cli``) MUST NOT pull the heavy ``playwright`` SDK into
``sys.modules``. The SDK import lives INSIDE the default page factory, which is
only reached when a stage actually navigates. This mirrors the Gemini /
ffmpeg / elevenlabs lazy-client discipline (Slice 1 import-purity invariant).

These assertions run in a FRESH interpreter (subprocess) so an unrelated test
that already imported playwright cannot mask a real leak.
"""

from __future__ import annotations

import subprocess
import sys


def _assert_playwright_not_loaded(import_stmt: str) -> None:
    probe = (
        f"{import_stmt}\n"
        "import sys\n"
        "leaked = sorted(m for m in sys.modules "
        "if m == 'playwright' or m.startswith('playwright.'))\n"
        "assert not leaked, f'playwright leaked at import: {leaked}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess exited {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_import_playwright_client_module_is_lazy() -> None:
    _assert_playwright_not_loaded("import shipcast.clients.playwright_client")


def test_import_cli_does_not_load_playwright() -> None:
    _assert_playwright_not_loaded("import shipcast.cli")


def test_import_clients_package_does_not_load_playwright() -> None:
    _assert_playwright_not_loaded("import shipcast.clients")
