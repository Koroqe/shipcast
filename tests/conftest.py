"""Shared pytest fixtures across the shipcast test suite.

Provides `tmp_project_root` (a fresh `tmp_path`-based projects root per test),
`sample_settings` (a default `Settings` for constructing projects), and a
`make_project` factory that materializes a bare project from the on-disk
template into a tmp root.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from shipcast.config import Settings
from shipcast.paths import default_template_path
from shipcast.project import Project

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def sample_settings() -> Settings:
    """A default `Settings` instance for tests that construct a `Project`.

    `settings` is a required keyword-only argument on `Project.create`. Tests
    exercising real `SecretStr` behavior monkeypatch env vars and build their own.
    """
    return Settings()


@pytest.fixture
def tmp_project_root(tmp_path: Path) -> Iterator[Path]:
    """A fresh empty directory representing a `projects/` root for one test.

    Tests that need an actual project subdirectory create it via `make_project`
    (or directly under `tmp_project_root / "<slug>"`).
    """
    yield tmp_path


@pytest.fixture
def fixtures_root() -> Path:
    """Absolute path to the `tests/fixtures/` directory."""
    return FIXTURES_ROOT


@pytest.fixture
def make_project(
    tmp_project_root: Path, sample_settings: Settings
) -> Callable[..., Project]:
    """Factory: materialize a bare project from the template into the tmp root.

    Returns a callable `make_project(slug="entry", **kwargs) -> Project`. The
    project starts with all 11 stages `pending` (seeded from the template
    manifest). `kwargs` forward to `Project.create` (e.g. `entry=`, `force=`).
    """

    def _make(slug: str = "entry", **kwargs: object) -> Project:
        return Project.create(
            tmp_project_root,
            slug,
            {},  # empty config_snapshot
            settings=sample_settings,
            template_path=default_template_path(),
            **kwargs,  # type: ignore[arg-type]
        )

    return _make
