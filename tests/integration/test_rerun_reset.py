"""`--rerun` and `reset` integration tests (cascade-confirmation guard).

Owned TCs:
- TC-16.1: `--rerun` on a done stage resets to pending and re-executes.
- TC-16.2: `--rerun` on a pending/failed stage is a no-op and runs normally.
- TC-16.3: `--rerun` on a truly-running stage raises StageBusy / refuses.
- TC-16.4: `--rerun` with downstream approvals — cascade guard prompts (no --yes).
- TC-16.5: `--rerun` with downstream approvals — `--yes` bypasses the prompt.
- TC-16.6: operator types `n` at the cascade prompt — no modification.
- TC-16.7: `reset` deletes outputs and resets downstream transitively.
- TC-16.8: `reset` without `--yes` prompts for confirmation.
- TC-16.9: `reset` when an output file is missing on disk — continues with a warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import shipcast.cli as cli
import shipcast.stages as _stages
from shipcast.config import Settings
from shipcast.manifest import Manifest, StageStatus
from shipcast.paths import default_template_path
from shipcast.project import Project
from tests._fakestage import DownstreamStage, FakeStage, output_path

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.create(
        tmp_path, "entry", {}, settings=Settings(), template_path=default_template_path()
    )


@pytest.fixture(autouse=True)
def _inject_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_stages, "ALL_STAGES", (FakeStage, DownstreamStage))


def _root(project: Project) -> list[str]:
    return ["--projects-root", str(project.root)]


def _pick_and_approve(project: Project) -> None:
    assert runner.invoke(cli.app, [*_root(project), "pick", "entry"]).exit_code == 0
    assert (
        runner.invoke(cli.app, [*_root(project), "approve", "entry", "01_pick"]).exit_code
        == 0
    )


def _enrich_and_approve(project: Project) -> None:
    assert runner.invoke(cli.app, [*_root(project), "enrich", "entry"]).exit_code == 0
    assert (
        runner.invoke(cli.app, [*_root(project), "approve", "entry", "02_enrich"]).exit_code
        == 0
    )


# --------------------------------------------------------------------------- #
# TC-16.1 / TC-16.2 — basic rerun semantics
# --------------------------------------------------------------------------- #


def test_tc_16_1_rerun_done_stage_resets_and_reexecutes(project: Project) -> None:
    """TC-16.1: --rerun on a done (unapproved) stage resets to pending then re-runs to done."""
    assert runner.invoke(cli.app, [*_root(project), "pick", "entry"]).exit_code == 0
    result = runner.invoke(cli.app, [*_root(project), "pick", "entry", "--rerun"])
    assert result.exit_code == 0, result.output
    m = Manifest.load(project.manifest_path)
    assert m.stages["01_pick"].status == StageStatus.DONE
    assert output_path(project).is_file()


def test_tc_16_2_rerun_pending_stage_is_noop_and_runs(project: Project) -> None:
    """TC-16.2: --rerun on a pending stage just runs normally (no-op reset)."""
    result = runner.invoke(cli.app, [*_root(project), "pick", "entry", "--rerun"])
    assert result.exit_code == 0, result.output
    m = Manifest.load(project.manifest_path)
    assert m.stages["01_pick"].status == StageStatus.DONE


# --------------------------------------------------------------------------- #
# TC-16.4 / TC-16.5 / TC-16.6 — cascade-confirmation guard on --rerun
# --------------------------------------------------------------------------- #


def test_tc_16_4_rerun_with_downstream_approval_shows_cascade_guard(project: Project) -> None:
    """TC-16.4: --rerun of 01_pick with 02_enrich approved triggers the cascade guard.

    CliRunner's stdin is NOT a tty, so the guard cannot prompt interactively; it
    lists the at-risk approvals and refuses with exit 1 unless --yes is passed
    (this is the non-tty branch of the cascade-confirmation guard). The manifest
    is left unchanged.
    """
    _pick_and_approve(project)
    _enrich_and_approve(project)
    before = project.manifest_path.read_bytes()
    result = runner.invoke(cli.app, [*_root(project), "pick", "entry", "--rerun"])
    assert result.exit_code == cli._EXIT_USER_ERROR, result.output
    assert "discard approvals" in result.output
    assert "02_enrich" in result.output
    # Manifest untouched: 02_enrich approval survives the refused cascade.
    assert project.manifest_path.read_bytes() == before
    m = Manifest.load(project.manifest_path)
    assert m.stages["02_enrich"].human_approved_at is not None


def test_tc_16_5_rerun_with_yes_bypasses_prompt(project: Project) -> None:
    """TC-16.5: --yes bypasses the cascade prompt and proceeds."""
    _pick_and_approve(project)
    _enrich_and_approve(project)
    result = runner.invoke(
        cli.app, [*_root(project), "pick", "entry", "--rerun", "--yes"]
    )
    assert result.exit_code == 0, result.output
    m = Manifest.load(project.manifest_path)
    assert m.stages["02_enrich"].status == StageStatus.PENDING


def test_tc_16_6_cascade_guard_returns_false_on_decline(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-16.6: when the operator declines the interactive cascade prompt, the guard returns False.

    CliRunner's stdin is not a real tty, so we drive the interactive branch
    directly: force `isatty` True and stub `Confirm.ask` to return False
    (operator typed 'n'). `_confirm_cascade` must return False → the dispatcher
    aborts without modifying the manifest.
    """
    _pick_and_approve(project)
    _enrich_and_approve(project)
    project = Project.load(project.root, "entry")

    class _FakeStdin:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr("shipcast.cli.sys.stdin", _FakeStdin())
    monkeypatch.setattr("shipcast.cli.Confirm.ask", staticmethod(lambda *a, **k: False))

    decision = cli._confirm_cascade(
        project, "01_pick", yes=False, action_verb="shipcast pick --rerun"
    )
    assert decision is False  # operator declined → no modification


def test_tc_16_6_cascade_guard_returns_true_on_confirm(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-16.6 (companion): confirming the interactive prompt returns True (proceed)."""
    _pick_and_approve(project)
    _enrich_and_approve(project)
    project = Project.load(project.root, "entry")

    class _FakeStdin:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr("shipcast.cli.sys.stdin", _FakeStdin())
    monkeypatch.setattr("shipcast.cli.Confirm.ask", staticmethod(lambda *a, **k: True))

    decision = cli._confirm_cascade(
        project, "01_pick", yes=False, action_verb="shipcast pick --rerun"
    )
    assert decision is True


# --------------------------------------------------------------------------- #
# TC-16.7 / TC-16.8 / TC-16.9 — reset
# --------------------------------------------------------------------------- #


def test_tc_16_7_reset_deletes_outputs_and_cascades(project: Project) -> None:
    """TC-16.7: reset 01_pick deletes its outputs and cascade-resets 02_enrich."""
    _pick_and_approve(project)
    _enrich_and_approve(project)
    out = output_path(project)
    assert out.is_file()

    result = runner.invoke(cli.app, [*_root(project), "reset", "entry", "01_pick", "--yes"])
    assert result.exit_code == 0, result.output
    assert not out.exists()  # declared output deleted
    m = Manifest.load(project.manifest_path)
    assert m.stages["01_pick"].status == StageStatus.PENDING
    assert m.stages["02_enrich"].status == StageStatus.PENDING


def test_tc_16_8_reset_without_yes_prompts(project: Project) -> None:
    """TC-16.8: reset with downstream approvals prompts; answering 'n' aborts."""
    _pick_and_approve(project)
    _enrich_and_approve(project)
    before = project.manifest_path.read_bytes()
    result = runner.invoke(
        cli.app, [*_root(project), "reset", "entry", "01_pick"], input="n\n"
    )
    assert result.exit_code == cli._EXIT_USER_ERROR, result.output
    assert "discard approvals" in result.output
    assert project.manifest_path.read_bytes() == before


def test_tc_16_9_reset_missing_output_file_continues(project: Project) -> None:
    """TC-16.9: reset continues (no crash) when a declared output is already gone."""
    assert runner.invoke(cli.app, [*_root(project), "pick", "entry"]).exit_code == 0
    # Operator deletes the file out of band before reset.
    output_path(project).unlink()
    result = runner.invoke(cli.app, [*_root(project), "reset", "entry", "01_pick", "--yes"])
    assert result.exit_code == 0, result.output
    m = Manifest.load(project.manifest_path)
    assert m.stages["01_pick"].status == StageStatus.PENDING
