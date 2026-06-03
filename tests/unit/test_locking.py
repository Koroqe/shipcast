"""Locking + platform-guard tests.

Owned TCs:
- TC-18.1: two-process lock contention → exactly one exit-0, one exit-4 (ProjectLocked).
- TC-18.2: `--no-lock` without SHIPCAST_NO_LOCK_ACK=1 → LockBypassNotAcknowledged, exit 4.
- TC-18.4: lock released on clean exit → a subsequent `acquire` succeeds.
- TC-2.5: non-macOS/Linux platform → UnsupportedPlatform before any FS op.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import shipcast.cli as cli
import shipcast.locking as locking
import shipcast.stages as _stages
from shipcast.config import Settings
from shipcast.errors import ProjectLocked, UnsupportedPlatform
from shipcast.locking import acquire, check_platform
from shipcast.paths import default_template_path
from shipcast.project import Project
from tests._fakestage import FakeStage

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# TC-2.5 — platform guard
# --------------------------------------------------------------------------- #


def test_tc_2_5_unsupported_platform_raises_before_fs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-2.5: a non-macOS/Linux sys.platform raises UnsupportedPlatform at the guard."""
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(UnsupportedPlatform):
        check_platform()


def test_tc_2_5_status_command_refuses_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TC-2.5: `shipcast status` aborts on win32 before touching the filesystem."""
    monkeypatch.setattr(sys, "platform", "win32")
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["--projects-root", str(tmp_path), "status", "nope"]
    )
    assert result.exit_code != 0
    # No project lookup happened (UnsupportedPlatform raised first).
    assert not (tmp_path / "nope").exists()


# --------------------------------------------------------------------------- #
# TC-18.4 — lock acquire/release round-trip
# --------------------------------------------------------------------------- #


def test_tc_18_4_lock_released_on_clean_exit(tmp_path: Path) -> None:
    """TC-18.4: after a clean `acquire` exit, the lock can be re-acquired."""
    lock_path = tmp_path / ".lock"
    with acquire(lock_path):
        pass
    # File is unlinked on clean exit; a fresh acquire succeeds.
    assert not lock_path.exists()
    with acquire(lock_path):
        pass


def test_lock_contention_in_process_raises_project_locked(tmp_path: Path) -> None:
    """Holding the lock then re-acquiring on a second fd raises ProjectLocked."""
    lock_path = tmp_path / ".lock"
    with acquire(lock_path):
        with pytest.raises(ProjectLocked):
            with acquire(lock_path):
                pass


# --------------------------------------------------------------------------- #
# TC-18.2 — --no-lock without acknowledgement
# --------------------------------------------------------------------------- #


def _seed_project(tmp_path: Path) -> Project:
    return Project.create(
        tmp_path,
        "entry",
        {},
        settings=Settings(),
        template_path=default_template_path(),
    )


def test_tc_18_2_no_lock_without_ack_exits_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-18.2: --no-lock without SHIPCAST_NO_LOCK_ACK=1 → exit 4 (concurrency)."""
    _seed_project(tmp_path)

    # Inject a fake stage so `pick` resolves past the not-implemented guard and
    # reaches the lock-bypass guard.
    monkeypatch.setattr(_stages, "ALL_STAGES", (FakeStage,))
    monkeypatch.delenv("SHIPCAST_NO_LOCK_ACK", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["--projects-root", str(tmp_path), "--no-lock", "pick", "entry"]
    )
    assert result.exit_code == cli._EXIT_CONCURRENCY, result.output


# --------------------------------------------------------------------------- #
# TC-18.1 — two-process lock contention (the one allowed subprocess test)
# --------------------------------------------------------------------------- #


def test_tc_18_1_two_process_race_one_winner_one_locked(tmp_path: Path) -> None:
    """TC-18.1: two concurrent `shipcast pick` procs → exactly one exit-0, one exit-4.

    Uses `subprocess.Popen` + a `pre_run_hook` sleep (injected in
    `tests/_race_driver.py`) to widen the locked window. No env-var sleep in
    production code (testing rule).
    """
    _seed_project(tmp_path)

    cmd = [
        sys.executable,
        "-m",
        "tests._race_driver",
        str(tmp_path),
        "entry",
    ]
    env_extra = {"PYTHONPATH": str(REPO_ROOT / "src") + ":" + str(REPO_ROOT)}
    env = {**os.environ, **env_extra}

    p1 = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env)
    time.sleep(0.2)  # ensure p1 grabs the lock first
    p2 = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env)
    rc1 = p1.wait(timeout=30)
    rc2 = p2.wait(timeout=30)

    codes = sorted([rc1, rc2])
    assert codes == [0, cli._EXIT_CONCURRENCY], f"got exit codes {codes!r}"


def test_locking_module_has_supported_platforms() -> None:
    """darwin and linux are the supported platforms (documents the contract)."""
    assert "darwin" in locking.SUPPORTED_PLATFORMS
    assert "linux" in locking.SUPPORTED_PLATFORMS
