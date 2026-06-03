"""Human-gate integration tests (dispatch → approve → downstream).

Owned TCs:
- TC-15.1: downstream refuses when upstream is done but NOT approved.
- TC-15.2: downstream refuses when upstream is not done.
- TC-15.3: `shipcast approve` when stage is not done → exit 1.
- TC-15.4: `shipcast approve` without edits → manually_edited=false.
- TC-15.5: `shipcast approve` after editing output → manually_edited=true, files listed.
- TC-15.6: downstream stage runs only after the upstream is approved.
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
    """Inject the two fake stages (01_pick → 02_enrich) for every test here."""
    monkeypatch.setattr(_stages, "ALL_STAGES", (FakeStage, DownstreamStage))


def _root(project: Project) -> list[str]:
    return ["--projects-root", str(project.root)]


def _run_pick(project: Project) -> None:
    result = runner.invoke(cli.app, [*_root(project), "pick", "entry"])
    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------- #
# TC-15.4 — approve without edits
# --------------------------------------------------------------------------- #


def test_tc_15_4_approve_without_edits_not_marked_edited(project: Project) -> None:
    """TC-15.4: approving an unmodified done stage records manually_edited=false."""
    _run_pick(project)
    result = runner.invoke(cli.app, [*_root(project), "approve", "entry", "01_pick"])
    assert result.exit_code == 0, result.output
    m = Manifest.load(project.manifest_path)
    rec = m.stages["01_pick"]
    assert rec.human_approved_at is not None
    assert rec.manually_edited is False


# --------------------------------------------------------------------------- #
# TC-15.5 — approve after editing output
# --------------------------------------------------------------------------- #


def test_tc_15_5_approve_after_edit_marks_edited_and_lists_file(project: Project) -> None:
    """TC-15.5: hand-editing the output then approving sets manually_edited=true + lists files."""
    _run_pick(project)
    # Operator edits the declared output between run and approve.
    out = output_path(project)
    out.write_text('{\n  "ok": false\n}\n', encoding="utf-8")

    result = runner.invoke(cli.app, [*_root(project), "approve", "entry", "01_pick"])
    assert result.exit_code == 0, result.output
    assert "manually_edited=true" in result.output
    assert "entry.json" in result.output

    m = Manifest.load(project.manifest_path)
    assert m.stages["01_pick"].manually_edited is True


# --------------------------------------------------------------------------- #
# TC-15.3 — approve when not done
# --------------------------------------------------------------------------- #


def test_tc_15_3_approve_when_not_done_exits_user_error(project: Project) -> None:
    """TC-15.3: approving a stage that is still pending exits 1."""
    result = runner.invoke(cli.app, [*_root(project), "approve", "entry", "01_pick"])
    assert result.exit_code == cli._EXIT_USER_ERROR, result.output
    assert "cannot approve" in result.output


# --------------------------------------------------------------------------- #
# TC-15.1 / TC-15.2 / TC-15.6 — downstream gate
# --------------------------------------------------------------------------- #


def test_tc_15_2_downstream_refuses_when_upstream_not_done(project: Project) -> None:
    """TC-15.2: running 02_enrich before 01_pick is done → FAILED (StageInputMissing)."""
    result = runner.invoke(cli.app, [*_root(project), "enrich", "entry"])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output
    m = Manifest.load(project.manifest_path)
    assert m.stages["02_enrich"].status == StageStatus.FAILED
    assert m.stages["02_enrich"].error is not None
    assert m.stages["02_enrich"].error.type == "StageInputMissing"


def test_tc_15_1_downstream_refuses_when_upstream_unapproved(project: Project) -> None:
    """TC-15.1: 01_pick done but unapproved → 02_enrich refuses (StageNotApproved)."""
    _run_pick(project)  # 01_pick now DONE but NOT approved
    result = runner.invoke(cli.app, [*_root(project), "enrich", "entry"])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output
    m = Manifest.load(project.manifest_path)
    assert m.stages["02_enrich"].status == StageStatus.FAILED
    assert m.stages["02_enrich"].error is not None
    assert m.stages["02_enrich"].error.type == "StageNotApproved"


def test_tc_15_6_downstream_runs_after_upstream_approved(project: Project) -> None:
    """TC-15.6: once 01_pick is approved, 02_enrich runs to DONE."""
    _run_pick(project)
    approve = runner.invoke(cli.app, [*_root(project), "approve", "entry", "01_pick"])
    assert approve.exit_code == 0, approve.output

    result = runner.invoke(cli.app, [*_root(project), "enrich", "entry"])
    assert result.exit_code == 0, result.output
    m = Manifest.load(project.manifest_path)
    assert m.stages["02_enrich"].status == StageStatus.DONE


# --------------------------------------------------------------------------- #
# Review checklist (printed after a successful run)
# --------------------------------------------------------------------------- #


def test_review_checklist_printed_with_abs_path_and_bullets(project: Project) -> None:
    """A successful run prints the Review Checklist with the artifact path + bullets."""
    result = runner.invoke(cli.app, [*_root(project), "pick", "entry"])
    assert result.exit_code == 0, result.output
    # Rich wraps the panel at the console width, so assert on individual tokens
    # rather than a contiguous command string.
    assert "Review checklist" in result.output
    assert "01_pick" in result.output
    assert "entry.json" in result.output
    assert "approve" in result.output
    assert "Rerun" in result.output
    assert "Reset" in result.output
