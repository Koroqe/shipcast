"""Unit tests for `PickStage` internals (Slice 6).

Cover the stage's defensive paths directly (without the CLI dispatcher):
- `check_inputs` raises `StageInputMissing` when `input.yaml` is absent.
- `run()` rejects a non-mapping `input.yaml`.
- `_safe_repo_path` enforces the path-traversal / allowed-root guards BEFORE any
  CHANGELOG read (mirroring the security-critical InputYaml validators).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import shipcast.schemas as schemas
from shipcast.config import Settings
from shipcast.errors import StageInputMissing
from shipcast.paths import default_template_path
from shipcast.project import Project
from shipcast.stages.s01_pick import PickStage


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.create(
        tmp_path / "projects",
        "entry",
        {},
        settings=Settings(),
        template_path=default_template_path(),
    )


def test_check_inputs_raises_when_input_yaml_missing(project: Project) -> None:
    """check_inputs raises StageInputMissing when input.yaml is absent."""
    project.input_path.unlink()
    with pytest.raises(StageInputMissing):
        PickStage().check_inputs(project)


def test_check_inputs_passes_when_input_yaml_present(project: Project) -> None:
    """check_inputs is a no-op when input.yaml exists (template seeds one)."""
    assert project.input_path.is_file()
    PickStage().check_inputs(project)  # must not raise


def test_run_rejects_non_mapping_input_yaml(project: Project) -> None:
    """A YAML scalar (not a mapping) raises ValueError before any CHANGELOG read."""
    project.input_path.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a YAML mapping"):
        PickStage().run(project)


def test_safe_repo_path_missing_field_raises() -> None:
    """_safe_repo_path raises StageInputMissing when repo_path is absent."""
    with pytest.raises(StageInputMissing, match="repo_path"):
        PickStage._safe_repo_path({})


def test_safe_repo_path_rejects_dotdot_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A '..' segment in repo_path is rejected before any filesystem access."""
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", tmp_path)
    with pytest.raises(ValueError, match=r"'\.\.' segments"):
        PickStage._safe_repo_path({"repo_path": str(tmp_path / ".." / "evil")})


def test_safe_repo_path_rejects_outside_allowed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo_path outside ALLOWED_REPO_ROOT is rejected (literal check)."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", allowed)
    with pytest.raises(ValueError, match="must be under"):
        PickStage._safe_repo_path({"repo_path": str(outside)})


def test_safe_repo_path_rejects_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink under the allowed root pointing outside is rejected after resolution."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed / "link"
    link.symlink_to(outside)
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", allowed)
    with pytest.raises(ValueError, match="resolves outside"):
        PickStage._safe_repo_path({"repo_path": str(link)})


def test_safe_repo_path_accepts_path_under_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean path under the allowed root resolves successfully."""
    allowed = tmp_path / "allowed"
    repo = allowed / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", allowed)
    result = PickStage._safe_repo_path({"repo_path": str(repo)})
    assert result == repo.resolve()
