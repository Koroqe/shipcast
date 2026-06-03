"""Project — handle to a single `projects/<slug>/` folder with its manifest.

`Project` is a thin, frozen value object. The CLI dispatcher (Slice 6) is the
only caller that mutates state via the helpers here:

* `Project.create(...)` materializes a fresh project folder from the on-disk
  template, validates and writes the manifest, and returns a Project.
* `Project.load(...)` reads the manifest off disk and returns a Project.
* `Project.with_manifest(new_manifest)` returns a new Project sharing the same
  root/slug but pointing at a different Manifest instance.

Stages and clients NEVER mutate the manifest directly; the dispatcher saves it
explicitly via `project.save_manifest()`.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from shipcast.config import Settings
from shipcast.errors import InvalidSlug, ProjectExists, ProjectNotFound
from shipcast.manifest import Manifest
from shipcast.paths import STAGE_DIR_NAMES, default_template_path

_SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _validate_slug(slug: str) -> None:
    """Reject slugs that would be unsafe as directory names.

    Allowed: ASCII alphanumerics, hyphen, underscore. Must start with an
    alphanumeric and be non-empty. Anything else raises `InvalidSlug`.
    """
    if not _SLUG_PATTERN.match(slug):
        raise InvalidSlug(
            f"slug {slug!r} is invalid; must match ^[a-zA-Z0-9][a-zA-Z0-9_-]*$"
        )


@dataclass(frozen=True)
class Project:
    """Handle to a marketing project on disk.

    The `settings` field carries the live `Settings` instance — the only
    in-memory channel through which stages reach the operator's API keys
    (architect BLOCKER-1 resolution, option (a)). It is NEVER serialized into
    `manifest.json`; secrets travel through the process environment, not the
    on-disk manifest. See `.claude/rules/security.md` (NFR-2.2).
    """

    root: Path
    slug: str
    manifest: Manifest
    settings: Settings

    # ------------------------------------------------------------------ paths

    @property
    def path(self) -> Path:
        """Path to this project's folder: `<root>/<slug>/`."""
        return self.root / self.slug

    @property
    def manifest_path(self) -> Path:
        """Path to this project's `manifest.json`."""
        return self.path / "manifest.json"

    @property
    def input_path(self) -> Path:
        """Path to this project's operator-supplied `input.yaml`."""
        return self.path / "input.yaml"

    def stage_dir(self, stage_id: str) -> Path:
        """Return the on-disk subdirectory for the given stage id."""
        try:
            return self.path / STAGE_DIR_NAMES[stage_id]
        except KeyError as exc:
            raise KeyError(f"unknown stage_id {stage_id!r}") from exc

    def artifact_path(self, stage_id: str, name: str) -> Path:
        """Return the path of an artifact `<name>` inside `stage_dir(stage_id)`."""
        return self.stage_dir(stage_id) / name

    # ------------------------------------------------------------------ create / load

    @classmethod
    def create(
        cls,
        root: Path,
        slug: str,
        config_snapshot: dict[str, Any],
        *,
        settings: Settings,
        entry: dict[str, Any] | None = None,
        force: bool = False,
        template_path: Path | None = None,
    ) -> Project:
        """Materialize a new project on disk by copying the template.

        `settings` is REQUIRED (keyword-only). The caller (the `pick` stage in
        Slice 6) constructs it once and passes it in; it becomes the project's
        in-memory accessor for API keys (architect BLOCKER-1 resolution). The
        settings instance is NOT serialized into `manifest.json` — only the
        public `config_snapshot` dict is.

        `entry` is the optional changelog entry dict (populated by Slice 6's
        `pick` stage). When None it is left as `null` in the seeded manifest.

        Steps:
        1. Validate `slug`.
        2. Refuse if `<root>/<slug>/` already exists, unless `force=True`
           (in which case the existing folder is removed first).
        3. Copy the template directory tree to `<root>/<slug>/`.
        4. Update the (template-seeded) manifest with the supplied slug,
           entry, timestamps, and config_snapshot, and save it back.
        """
        _validate_slug(slug)
        target = root / slug
        if target.exists():
            if not force:
                raise ProjectExists(
                    f"project folder already exists at {target!r}; pass force=True to overwrite"
                )
            shutil.rmtree(target)
        template = template_path or default_template_path()
        try:
            shutil.copytree(template, target)
        except FileExistsError as exc:
            # Race: another `shipcast new` call won the copytree between our
            # exists() check and copytree() call. Surface a stable error name.
            raise ProjectExists(f"project folder appeared at {target!r} during create") from exc

        seed_manifest = Manifest.load(target / "manifest.json")
        now = datetime.now(UTC)
        new_manifest = seed_manifest.model_copy(
            update={
                "slug": slug,
                "entry": entry,
                "created_at": now,
                "updated_at": now,
                "config_snapshot": dict(config_snapshot),
            }
        )
        new_manifest.save(target / "manifest.json")
        return cls(root=root, slug=slug, manifest=new_manifest, settings=settings)

    @classmethod
    def load(cls, root: Path, slug: str) -> Project:
        """Read a project's manifest from disk and return a Project.

        Re-populates `settings` from the repo's `config.toml` + `.env`
        (both cwd-relative, matching the CLI's invocation contract: the
        shipcast CLI is always invoked from the repo root). Reading
        `config.toml` LIVE on every load is what makes the "tuning knob"
        contract real for `voice_id` / `*_model` / `target_duration_sec`
        — without it, stage code that reads `project.settings.voice_id`
        gets the field default no matter what the operator wrote in
        `config.toml`. Secrets still live in env vars (loaded from `.env`
        by pydantic-settings at construction time), NOT in `manifest.json`.
        See architect BLOCKER-1 (option (a) — Settings round-trips
        through env, not through the manifest).

        Raises `ProjectNotFound` if the folder is missing. `Manifest.load`
        bubbles `ManifestCorrupt` / `ManifestMigrationNeeded` on bad data.
        """
        target = root / slug
        if not target.is_dir():
            raise ProjectNotFound(f"no project folder at {target!r}")
        manifest = Manifest.load(target / "manifest.json")
        settings = Settings.from_files(
            config_path=Path("config.toml"),
            env_path=Path(".env"),
        )
        return cls(root=root, slug=slug, manifest=manifest, settings=settings)

    # ------------------------------------------------------------------ mutation helpers

    def with_manifest(self, new_manifest: Manifest) -> Project:
        """Return a new Project pointing at a different Manifest instance.

        The on-disk state is NOT changed — call `save_manifest()` to persist.
        """
        return replace(self, manifest=new_manifest)

    def save_manifest(self) -> None:
        """Persist `self.manifest` to disk."""
        self.manifest.save(self.manifest_path)
