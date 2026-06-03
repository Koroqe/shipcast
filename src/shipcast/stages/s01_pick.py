"""Stage 01 — pick.

The first real pipeline stage. Reads the operator-supplied
``projects/<slug>/input.yaml``, validates it against :class:`InputYaml`
(URL/path SSRF + traversal defenses from Slice 3), parses the *target* repo's
``CHANGELOG.md`` (via :mod:`shipcast.changelog.parser`), locates the requested
entry by a trimmed/case-insensitive heading match, and writes the deterministic
``01_pick/entry.json`` artifact.

shipcast NEVER writes into the target repo — it only *reads* its
``CHANGELOG.md``. The parser raises :class:`ChangelogFileMissing` (never
auto-creating the file) and this stage raises :class:`ChangelogEntryNotFound`
when the heading is absent.

The stage is a pure function of its inputs: the dispatcher owns the manifest
write, the lock, and the human gate. ``run()`` only reads inputs and writes the
artifact inside its own ``stage_dir``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import yaml

from shipcast.changelog.parser import find_entry, parse_changelog
from shipcast.errors import ChangelogEntryNotFound, StageInputMissing
from shipcast.manifest import StageStatus, dump_json_canonical
from shipcast.schemas import (
    ChangelogEntry,
    InputYaml,
    _is_relative_to,
)
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from shipcast.project import Project


class PickStage(BaseStage):
    """Ingest the target repo's CHANGELOG entry into ``01_pick/entry.json``."""

    id: ClassVar[str] = "01_pick"
    requires: ClassVar[tuple[str, ...]] = ()
    output_schema: ClassVar[type[ChangelogEntry]] = ChangelogEntry
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Confirm the picked changelog entry matches the requested heading.",
        "Verify entry.json's summary and details read the way you want them quoted downstream.",
        "Check the date and (optional) UTC time fields are the ones you expect.",
    )

    OUTPUT_FILENAME: ClassVar[str] = "entry.json"

    def check_inputs(self, project: Project) -> None:
        """Verify ``input.yaml`` exists. Stage 01 has no upstream stages.

        Runs BEFORE ``run()`` (the dispatcher enforces this ordering), so a
        missing ``input.yaml`` raises :class:`StageInputMissing` and the stage
        never reaches its ``run()`` body.
        """
        if not project.input_path.is_file():
            raise StageInputMissing(
                f"stage {self.id!r} requires {project.input_path} to exist"
            )

    @staticmethod
    def _safe_repo_path(cleaned: dict[str, object]) -> Path:
        """Return the traversal-safe `repo_path` from the raw input mapping.

        Mirrors the path-safety subset of :class:`InputYaml`'s repo_path
        validator (reject ``..`` segments; require the literal AND symlink-
        resolved path to live under :data:`ALLOWED_REPO_ROOT`) so we never read
        a CHANGELOG outside the allowed root, even though full ``InputYaml``
        validation runs slightly later (to let ``ChangelogFileMissing`` take
        precedence over the InputYaml CHANGELOG-existence check at run time).

        Raises:
            StageInputMissing: if ``repo_path`` is absent from the input mapping.
            ValueError: on a ``..`` segment or an out-of-root path.
        """
        # Re-read ALLOWED_REPO_ROOT from the module each call so tests that
        # monkeypatch `shipcast.schemas.ALLOWED_REPO_ROOT` are honored.
        from shipcast import schemas as _schemas

        value = cleaned.get("repo_path")
        if value is None:
            raise StageInputMissing("input.yaml is missing the required 'repo_path' field")
        raw = Path(str(value))
        if ".." in raw.parts:
            raise ValueError("repo_path must not contain '..' segments")
        allowed_root = _schemas.ALLOWED_REPO_ROOT.resolve()
        if not _is_relative_to(raw, allowed_root):
            raise ValueError(f"repo_path must be under {allowed_root} (got {raw})")
        resolved = raw.resolve()
        if not _is_relative_to(resolved, allowed_root):
            raise ValueError(
                f"repo_path resolves outside the allowed root {allowed_root} "
                f"(resolved to {resolved})"
            )
        return resolved

    def run(self, project: Project) -> StageResult:
        """Read + validate ``input.yaml``, parse the target CHANGELOG, write ``entry.json``.

        Determinism: identical ``input.yaml`` + unchanged ``CHANGELOG.md`` yield
        byte-identical ``entry.json`` (the parser and ``dump_json_canonical`` are
        both pure functions of their inputs).
        """
        raw = project.input_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError(
                f"{project.input_path} must contain a YAML mapping, "
                f"got {type(data).__name__}"
            )
        # Drop keys whose value is None so optional-unspecified fields fall back
        # to their schema defaults instead of being explicitly set to None.
        cleaned = {k: v for k, v in data.items() if v is not None}

        # The CHANGELOG-missing signal at RUN time is `ChangelogFileMissing`
        # (UC-2-E1 / TC-4.9), distinct from the `ValidationError` raised when an
        # operator first authors an `input.yaml` pointing at a CHANGELOG-less
        # repo (TC-3.10, caught at InputYaml-parse time). The CHANGELOG can
        # legitimately disappear AFTER a valid input.yaml was written, so we
        # surface that case via the parser. Path-traversal safety MUST still gate
        # any filesystem access, so we apply the same `..`/allowed-root guard the
        # InputYaml validator uses BEFORE reading the repo.
        repo_path = self._safe_repo_path(cleaned)
        changelog_path = repo_path / "CHANGELOG.md"
        # parse_changelog raises ChangelogFileMissing if the file is gone; it
        # NEVER auto-creates it.
        entries = parse_changelog(changelog_path)

        # Full security validation (URL SSRF defense + path checks). The CHANGELOG
        # now exists (the parser would have raised otherwise), so InputYaml's
        # CHANGELOG-existence check passes here.
        spec = InputYaml.model_validate(cleaned)

        entry = find_entry(entries, spec.entry_heading)
        if entry is None:
            raise ChangelogEntryNotFound(
                f"no CHANGELOG entry matching heading {spec.entry_heading!r} "
                f"in {changelog_path} (parsed {len(entries)} entr"
                f"{'y' if len(entries) == 1 else 'ies'})"
            )

        output_path = project.artifact_path(self.id, self.OUTPUT_FILENAME)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            dump_json_canonical(entry.model_dump(mode="json")),
            encoding="utf-8",
        )

        return StageResult(
            status=StageStatus.DONE,
            outputs=(Path(self.id) / self.OUTPUT_FILENAME,),
            metrics={"entry_name": entry.name, "entry_date": entry.date},
        )
