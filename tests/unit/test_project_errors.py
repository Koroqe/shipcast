"""Unit tests for Project.create / Project.load error branches + with_manifest.

Covers lines 40, 82-83, 125-129, 133-136, 173 in project.py:
- InvalidSlug on bad slug characters
- ProjectExists without force=True
- ProjectExists with force=True overwrites the directory
- FileExistsError during copytree race → ProjectExists
- ProjectNotFound when folder does not exist
- with_manifest returns new Project with same root/slug
- save_manifest round-trip
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from shipcast.config import Settings
from shipcast.errors import InvalidSlug, ProjectExists, ProjectNotFound
from shipcast.paths import default_template_path
from shipcast.project import Project


def _settings() -> Settings:
    return Settings()


def _make(root: Path, slug: str = "my-project", **kwargs: object) -> Project:
    return Project.create(
        root,
        slug,
        {},
        settings=_settings(),
        template_path=default_template_path(),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# TC: InvalidSlug — leading non-alphanumeric
# ---------------------------------------------------------------------------


def test_invalid_slug_leading_hyphen(tmp_path: Path) -> None:
    with pytest.raises(InvalidSlug):
        _make(tmp_path, slug="-bad-slug")


def test_invalid_slug_empty_string(tmp_path: Path) -> None:
    with pytest.raises(InvalidSlug):
        _make(tmp_path, slug="")


def test_invalid_slug_space(tmp_path: Path) -> None:
    with pytest.raises(InvalidSlug):
        _make(tmp_path, slug="bad slug")


def test_invalid_slug_dot(tmp_path: Path) -> None:
    with pytest.raises(InvalidSlug):
        _make(tmp_path, slug="bad.slug")


# ---------------------------------------------------------------------------
# TC: ProjectExists without force → raises
# ---------------------------------------------------------------------------


def test_project_exists_without_force_raises(tmp_path: Path) -> None:
    _make(tmp_path, slug="entry")
    with pytest.raises(ProjectExists):
        _make(tmp_path, slug="entry")


# ---------------------------------------------------------------------------
# TC: ProjectExists with force=True → overwrites existing folder
# ---------------------------------------------------------------------------


def test_project_exists_with_force_overwrites(tmp_path: Path) -> None:
    p1 = _make(tmp_path, slug="entry")
    # Plant a sentinel file in the project folder
    sentinel = p1.path / "sentinel.txt"
    sentinel.write_text("old", encoding="utf-8")

    # force=True should wipe and recreate
    p2 = _make(tmp_path, slug="entry", force=True)
    assert not sentinel.exists(), "force=True must remove old project folder contents"
    assert p2.manifest_path.is_file()


# ---------------------------------------------------------------------------
# TC: copytree race (FileExistsError) → ProjectExists
# ---------------------------------------------------------------------------


def test_copytree_race_raises_project_exists(tmp_path: Path) -> None:
    """If copytree raises FileExistsError (race), Project.create wraps it as ProjectExists."""
    import shutil

    with patch.object(shutil, "copytree", side_effect=FileExistsError("race")):
        with pytest.raises(ProjectExists):
            _make(tmp_path, slug="race-slug")


# ---------------------------------------------------------------------------
# TC: ProjectNotFound when folder does not exist
# ---------------------------------------------------------------------------


def test_load_project_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFound):
        Project.load(tmp_path, "nonexistent")


# ---------------------------------------------------------------------------
# TC: with_manifest returns new Project, same root/slug
# ---------------------------------------------------------------------------


def test_with_manifest_returns_new_project_same_root_slug(tmp_path: Path) -> None:
    project = _make(tmp_path, slug="myslug")
    new_project = project.with_manifest(project.manifest)
    assert new_project.root == project.root
    assert new_project.slug == project.slug
    # Different object identity
    assert new_project is not project


# ---------------------------------------------------------------------------
# TC: save_manifest persists to disk
# ---------------------------------------------------------------------------


def test_save_manifest_writes_to_disk(tmp_path: Path) -> None:
    project = _make(tmp_path, slug="save-test")
    # Modify the in-memory manifest slug field (just verify save works)
    project.save_manifest()
    assert project.manifest_path.is_file()
    content = project.manifest_path.read_text(encoding="utf-8")
    assert "save-test" in content


# ---------------------------------------------------------------------------
# TC: valid slugs accepted (positive cases)
# ---------------------------------------------------------------------------


def test_valid_slug_alphanumeric(tmp_path: Path) -> None:
    p = _make(tmp_path, slug="abc123")
    assert p.slug == "abc123"


def test_valid_slug_with_hyphen_and_underscore(tmp_path: Path) -> None:
    p = _make(tmp_path, slug="my-proj_v2")
    assert p.slug == "my-proj_v2"


# ---------------------------------------------------------------------------
# TC: stage_dir raises KeyError for unknown stage_id
# ---------------------------------------------------------------------------


def test_stage_dir_unknown_stage_id_raises_key_error(tmp_path: Path) -> None:
    project = _make(tmp_path, slug="stages-test")
    with pytest.raises(KeyError, match="unknown stage_id"):
        project.stage_dir("99_nonexistent")
