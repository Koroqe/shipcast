"""Unit tests for `s02_enrich` pure helpers (Slice 7).

Covers the deterministic, network-free parts of the stage:
- `_parse_diffstat` over `git log --stat` summary lines.
- tolerant repo-signal collection (missing `gh`/`git` → empty, never raises).
- deterministic prompt building (no wall-clock / random — TC-21.3).

These complement the CLI-driven integration tests in `test_s02_enrich.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from shipcast.stages.s02_enrich import EnrichStage


def test_parse_diffstat_full_summary() -> None:
    stage = EnrichStage()
    out = stage._parse_diffstat(" 3 files changed, 42 insertions(+), 7 deletions(-)\n")
    assert out == {"files_changed": 3, "insertions": 42, "deletions": 7}


def test_parse_diffstat_insertions_only() -> None:
    stage = EnrichStage()
    out = stage._parse_diffstat(" 1 file changed, 5 insertions(+)\n")
    assert out == {"files_changed": 1, "insertions": 5, "deletions": 0}


def test_parse_diffstat_no_summary_line() -> None:
    stage = EnrichStage()
    out = stage._parse_diffstat("some unrelated text\n")
    assert out == {"files_changed": 0, "insertions": 0, "deletions": 0}


def test_collect_pr_links_gh_missing_returns_empty() -> None:
    """Missing `gh` binary (FileNotFoundError) → [] (graceful degrade, no raise)."""

    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        raise FileNotFoundError("gh not found")

    stage = EnrichStage(subprocess_run=_run)
    assert stage._collect_pr_links(Path("/tmp")) == []


def test_collect_pr_links_parses_json() -> None:
    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        return subprocess.CompletedProcess(
            cmd, 0, stdout='[{"url": "https://github.com/x/y/pull/9"}]', stderr=""
        )

    stage = EnrichStage(subprocess_run=_run)
    assert stage._collect_pr_links(Path("/tmp")) == ["https://github.com/x/y/pull/9"]


def test_collect_pr_links_nonzero_exit_returns_empty() -> None:
    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not a repo")

    stage = EnrichStage(subprocess_run=_run)
    assert stage._collect_pr_links(Path("/tmp")) == []


def test_collect_diff_stats_git_missing_returns_empty() -> None:
    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        raise FileNotFoundError("git not found")

    stage = EnrichStage(subprocess_run=_run)
    assert stage._collect_diff_stats(Path("/tmp")) == {}


def test_build_narrative_prompt_is_deterministic() -> None:
    """Same inputs → byte-identical prompt (no datetime / random — TC-21.3)."""
    entry = {"name": "Add CSV export", "summary": "s", "details": "d"}
    a = EnrichStage._build_narrative_prompt(entry, ["pr1"], {"files_changed": 2}, True)
    b = EnrichStage._build_narrative_prompt(entry, ["pr1"], {"files_changed": 2}, True)
    assert a == b
    assert "Add CSV export" in a
    assert "screenshots" in a  # has_screenshots branch reflected
