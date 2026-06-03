"""Integration tests for the `s01_pick` stage + the `pick` CLI verb (Slice 6).

Owned TCs:
- TC-4.7:  happy path — `shipcast pick <repo> --entry "<heading>"` creates the
           project, dispatches 01_pick to DONE, writes a schema-valid entry.json,
           outputs_hash_at_done populated, human_approved_at null.
- TC-4.8:  unknown heading → ChangelogEntryNotFound, status FAILED, no entry.json.
- TC-4.9:  CHANGELOG.md absent from the target repo → ChangelogFileMissing, FAILED.
- TC-4.10: input.yaml absent → StageInputMissing BEFORE run() (dispatch mode).
- TC-4.12 / TC-21.1: re-run on identical inputs → byte-identical entry.json.
- TC-4.14: Review Checklist printed (abs path + ≥3 bullets + rerun/approve/reset).

Project creation in `pick`: when `--entry` is supplied, the positional arg is a
target REPO PATH and the project is created (slug derived from repo + heading)
before dispatch. When `--entry` is omitted, the positional arg is an existing
project SLUG and the verb only dispatches. The integration tests exercise the
create path (the new Slice-6 acceptance criterion); the dispatch path is covered
by the Slice-1 human-gate / rerun-reset suites.

`InputYaml.repo_path` must live under `schemas.ALLOWED_REPO_ROOT`; tests
monkeypatch that constant to a tmp dir so the fixture repo validates.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import shipcast.cli as cli
import shipcast.schemas as schemas
from shipcast.config import Settings
from shipcast.manifest import Manifest, StageStatus
from shipcast.paths import default_template_path
from shipcast.project import Project
from shipcast.schemas import ChangelogEntry
from shipcast.stages.s01_pick import PickStage

runner = CliRunner()

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "repos" / "getdeal_min"
_CHANGELOG = (_FIXTURES / "CHANGELOG.md").read_text(encoding="utf-8")


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp dir that stands in for ALLOWED_REPO_ROOT, holding a target repo.

    Monkeypatches `schemas.ALLOWED_REPO_ROOT` so `InputYaml.repo_path` accepts
    the fixture repo (production allows only the real Projects.nosync root).
    """
    root = tmp_path / "repos_root"
    root.mkdir()
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", root)
    return root


@pytest.fixture
def target_repo(repo_root: Path) -> Path:
    """A minimal target repo with a canonical CHANGELOG.md."""
    repo = repo_root / "getdeal-platform-monorepo"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")
    return repo


def _root(projects_root: Path) -> list[str]:
    return ["--projects-root", str(projects_root)]


# --------------------------------------------------------------------------- #
# TC-4.7 — happy path (create mode)
# --------------------------------------------------------------------------- #


def test_tc_4_7_pick_creates_project_and_writes_entry_json(
    projects_root: Path, target_repo: Path
) -> None:
    """TC-4.7: pick <repo> --entry creates the project and writes a valid entry.json."""
    result = runner.invoke(
        cli.app,
        [*_root(projects_root), "pick", str(target_repo), "--entry", "Add CSV export"],
    )
    assert result.exit_code == 0, result.output

    # Slug derived as "<repo-short>--<entry-slug>".
    slug = "getdeal-platform-monorepo--add-csv-export"
    project_dir = projects_root / slug
    assert project_dir.is_dir()

    entry_path = project_dir / "01_pick" / "entry.json"
    assert entry_path.is_file()

    # Validates against the ChangelogEntry schema with the expected content.
    entry = ChangelogEntry.model_validate_json(entry_path.read_text(encoding="utf-8"))
    assert entry.name == "Add CSV export"
    assert entry.date == "2026-06-02"
    assert entry.time_utc == "14:30"
    assert entry.summary

    m = Manifest.load(project_dir / "manifest.json")
    rec = m.stages["01_pick"]
    assert rec.status == StageStatus.DONE
    assert rec.outputs == ("01_pick/entry.json",)
    assert rec.outputs_hash_at_done is not None
    assert rec.human_approved_at is None


# --------------------------------------------------------------------------- #
# TC-4.8 — heading not found
# --------------------------------------------------------------------------- #


def test_tc_4_8_unknown_heading_raises_entry_not_found(
    projects_root: Path, target_repo: Path
) -> None:
    """TC-4.8: an unknown heading → ChangelogEntryNotFound, FAILED, no entry.json."""
    result = runner.invoke(
        cli.app,
        [*_root(projects_root), "pick", str(target_repo), "--entry", "Nonexistent Feature"],
    )
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    slug = "getdeal-platform-monorepo--nonexistent-feature"
    project_dir = projects_root / slug
    m = Manifest.load(project_dir / "manifest.json")
    rec = m.stages["01_pick"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "ChangelogEntryNotFound"
    assert not (project_dir / "01_pick" / "entry.json").exists()


# --------------------------------------------------------------------------- #
# TC-4.9 — CHANGELOG.md missing from the target repo
# --------------------------------------------------------------------------- #


def test_tc_4_9_missing_changelog_raises_file_missing(
    projects_root: Path, target_repo: Path
) -> None:
    """TC-4.9: removing CHANGELOG.md after create → ChangelogFileMissing, FAILED.

    InputYaml validation requires CHANGELOG.md to exist at create time, so we
    delete it AFTER `Project.create` writes input.yaml but BEFORE the stage's
    run() re-reads it — exercising the parser's ChangelogFileMissing path. (The
    file is never auto-created.)
    """
    settings = Settings()
    slug = "getdeal-platform-monorepo--add-csv-export"
    project = Project.create(
        projects_root,
        slug,
        settings.public_dict(),
        settings=settings,
        template_path=default_template_path(),
    )
    project.input_path.write_text(
        "repo_path: '" + str(target_repo) + "'\n"
        "entry_heading: 'Add CSV export'\n"
        "brand_slug: 'getdeal'\n"
        "video_mode: 'standard'\n",
        encoding="utf-8",
    )
    # Remove the CHANGELOG so the stage's run() hits ChangelogFileMissing.
    (target_repo / "CHANGELOG.md").unlink()

    result = runner.invoke(cli.app, [*_root(projects_root), "pick", slug])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    m = Manifest.load(project.manifest_path)
    rec = m.stages["01_pick"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "ChangelogFileMissing"
    # The target repo's CHANGELOG was never recreated.
    assert not (target_repo / "CHANGELOG.md").exists()


# --------------------------------------------------------------------------- #
# TC-4.10 — input.yaml missing → StageInputMissing before run()
# --------------------------------------------------------------------------- #


def test_tc_4_10_missing_input_yaml_raises_before_run(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-4.10: removing input.yaml → check_inputs raises StageInputMissing, FAILED."""
    settings = Settings()
    slug = "no-input"
    project = Project.create(
        projects_root,
        slug,
        settings.public_dict(),
        settings=settings,
        template_path=default_template_path(),
    )
    # The template seeds an input.yaml; remove it to trigger the guard.
    project.input_path.unlink()
    assert not project.input_path.exists()

    # Guard run() so the test proves check_inputs fired FIRST (run never reached).
    def _boom(self: PickStage, project: Project) -> None:  # pragma: no cover - must not run
        raise AssertionError("run() must not be reached when input.yaml is missing")

    monkeypatch.setattr(PickStage, "run", _boom)

    result = runner.invoke(cli.app, [*_root(projects_root), "pick", slug])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    m = Manifest.load(project.manifest_path)
    rec = m.stages["01_pick"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "StageInputMissing"


# --------------------------------------------------------------------------- #
# TC-4.12 / TC-21.1 — idempotency (byte-identical entry.json on re-run)
# --------------------------------------------------------------------------- #


def test_tc_4_12_rerun_byte_identical_entry_json(
    projects_root: Path, target_repo: Path
) -> None:
    """TC-4.12 / TC-21.1: re-running on identical inputs yields byte-identical entry.json."""
    args = [*_root(projects_root), "pick", str(target_repo), "--entry", "Add CSV export"]
    assert runner.invoke(cli.app, args).exit_code == 0

    slug = "getdeal-platform-monorepo--add-csv-export"
    entry_path = projects_root / slug / "01_pick" / "entry.json"
    first = entry_path.read_bytes()

    # Re-run via dispatch mode (project already exists) with --rerun.
    result = runner.invoke(cli.app, [*_root(projects_root), "pick", slug, "--rerun"])
    assert result.exit_code == 0, result.output
    second = entry_path.read_bytes()
    assert first == second


def test_tc_4_12_matches_pinned_fixture(
    projects_root: Path, target_repo: Path
) -> None:
    """The written entry.json is byte-equal to the pinned fixture (determinism anchor)."""
    args = [*_root(projects_root), "pick", str(target_repo), "--entry", "Add CSV export"]
    assert runner.invoke(cli.app, args).exit_code == 0

    slug = "getdeal-platform-monorepo--add-csv-export"
    entry_path = projects_root / slug / "01_pick" / "entry.json"
    fixture = (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "repos"
        / "getdeal_min_entry.json"
    )
    assert entry_path.read_bytes() == fixture.read_bytes()


# --------------------------------------------------------------------------- #
# TC-4.14 — Review Checklist printed
# --------------------------------------------------------------------------- #


def test_tc_4_14_review_checklist_printed(
    projects_root: Path, target_repo: Path
) -> None:
    """TC-4.14: a successful pick prints the checklist (abs path, ≥3 bullets, next steps)."""
    result = runner.invoke(
        cli.app,
        [*_root(projects_root), "pick", str(target_repo), "--entry", "Add CSV export"],
    )
    assert result.exit_code == 0, result.output

    out = result.output
    assert "Review checklist" in out
    assert "entry.json" in out
    # At least 3 checklist bullets (PickStage declares exactly 3).
    assert out.count("•") >= 3
    # Next-step instructions cover rerun / approve / reset.
    assert "Rerun" in out
    assert "approve" in out
    assert "Reset" in out


# --------------------------------------------------------------------------- #
# Heading match is trimmed + case-insensitive (UC-2-A2 at the stage level)
# --------------------------------------------------------------------------- #


def test_pick_heading_match_is_trimmed_and_case_insensitive(
    projects_root: Path, target_repo: Path
) -> None:
    """A whitespace-padded, differently-cased --entry still resolves the entry."""
    result = runner.invoke(
        cli.app,
        [*_root(projects_root), "pick", str(target_repo), "--entry", "  add csv EXPORT  "],
    )
    assert result.exit_code == 0, result.output
    # Slug derived from the (slugified) padded heading.
    slug = "getdeal-platform-monorepo--add-csv-export"
    entry_path = projects_root / slug / "01_pick" / "entry.json"
    entry = ChangelogEntry.model_validate_json(entry_path.read_text(encoding="utf-8"))
    assert entry.name == "Add CSV export"
