"""Manifest data model, state transitions, and hash helpers.

This module is pure: no I/O outside of `Manifest.load` / `Manifest.save`. Stages
do not import from here directly; the CLI dispatcher (Slice 6) owns all manifest
mutations.

Two hash families are exposed and INTENTIONALLY asymmetric:

* `compute_inputs_hash` — fast SHA-256 over sorted `(rel_path, mtime_ns,
  size_bytes)` tuples. Used for `StageRecord.inputs_hash` (upstream
  invalidation). False positives only cost a re-run; false negatives are
  bounded because the upstream stage's `outputs_hash_at_done` already
  disambiguates same-mtime same-size byte changes.

* `compute_outputs_hash` — byte-content SHA-256 over sorted
  `(rel_path, sha256_of_file_bytes)` tuples. Used for
  `StageRecord.outputs_hash_at_done` and `shipcast approve`'s recomputation.
  Detects ANY manual edit reliably, including no-op-mtime-bumps and
  same-size-different-bytes swaps.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shipcast.errors import (
    CannotApproveNonDoneStage,
    ConfigSnapshotLocked,
    IllegalTransition,
    ManifestCorrupt,
    ManifestMigrationNeeded,
    UnknownStageId,
)

CURRENT_SCHEMA_VERSION: Final[int] = 1
SHA256_HEX_LEN: Final[int] = 64


class StageStatus(StrEnum):
    """Lifecycle status of a single stage within a project's manifest."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


_LEGAL_TRANSITIONS: Final[frozenset[tuple[StageStatus, StageStatus]]] = frozenset(
    {
        (StageStatus.PENDING, StageStatus.RUNNING),
        (StageStatus.RUNNING, StageStatus.DONE),
        (StageStatus.RUNNING, StageStatus.FAILED),
        (StageStatus.RUNNING, StageStatus.NEEDS_REVIEW),
        (StageStatus.NEEDS_REVIEW, StageStatus.RUNNING),
        (StageStatus.FAILED, StageStatus.RUNNING),
        (StageStatus.FAILED, StageStatus.PENDING),
        (StageStatus.DONE, StageStatus.PENDING),
    }
)


def is_legal_transition(from_status: StageStatus, to_status: StageStatus) -> bool:
    """Return True iff `(from_status, to_status)` is in the allowed transition matrix."""
    return (from_status, to_status) in _LEGAL_TRANSITIONS


class ErrorRecord(BaseModel):
    """Structured error captured when a stage transitions to `failed`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    message: str
    traceback_path: str | None = None


class StageRecord(BaseModel):
    """The persistent record for a single stage in a project's manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outputs: tuple[str, ...] = ()
    inputs_hash: str | None = None
    outputs_hash_at_done: str | None = None
    human_approved_at: datetime | None = None
    manually_edited: bool = False
    error: ErrorRecord | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class Manifest(BaseModel):
    """The single source of truth for a marketing project's pipeline state.

    The Manifest is frozen — every mutation returns a new instance. The CLI
    dispatcher is the only caller that creates new instances; stages do not
    mutate the manifest directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = CURRENT_SCHEMA_VERSION
    slug: str
    created_at: datetime
    updated_at: datetime
    entry: dict[str, Any] | None = None  # the changelog entry; populated by Slice 6
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    stages: dict[str, StageRecord] = Field(default_factory=dict)

    # ------------------------------------------------------------------ load / save

    @classmethod
    def load(cls, path: Path) -> Manifest:
        """Read and validate a manifest from disk.

        Raises `ManifestCorrupt` for unreadable/invalid JSON or schema violations.
        Raises `ManifestMigrationNeeded` when `schema_version` does not match
        `CURRENT_SCHEMA_VERSION`.
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestCorrupt(f"cannot read manifest at {path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManifestCorrupt(f"manifest at {path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ManifestCorrupt(f"manifest at {path} is not a JSON object")
        version = data.get("schema_version")
        if version != CURRENT_SCHEMA_VERSION:
            raise ManifestMigrationNeeded(
                f"manifest at {path} has schema_version={version!r}, expected {CURRENT_SCHEMA_VERSION}"
            )
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ManifestCorrupt(f"manifest at {path} failed validation: {exc}") from exc

    def save(self, path: Path) -> None:
        """Atomically write the manifest to `path`.

        Strategy: serialize to `<path>.tmp`, `fsync` the contents, `os.replace`
        into place. If the rename never runs (crash mid-write), the original
        manifest remains intact and only `<path>.tmp` is left on disk.
        """
        payload = self.serialize()
        tmp_path = path.with_name(path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def serialize(self) -> str:
        """Return the canonical JSON serialization of this manifest."""
        return dump_json_canonical(self.model_dump(mode="json"))

    # ------------------------------------------------------------------ transitions

    def transition(self, stage_id: str, new_status: StageStatus, **fields: Any) -> Manifest:
        """Return a new Manifest with `stage_id` advanced to `new_status`.

        Raises `IllegalTransition` if the move is not in the allowed matrix.
        Additional keyword arguments update other fields on the stage record
        atomically (e.g., `started_at=..., outputs=...`).
        """
        record = self._require_stage(stage_id)
        if not is_legal_transition(record.status, new_status):
            raise IllegalTransition(
                f"cannot transition stage {stage_id!r} from {record.status.value} to {new_status.value}"
            )
        new_record = record.model_copy(update={"status": new_status, **fields})
        return self._with_stage(stage_id, new_record)

    def approve(self, stage_id: str, current_outputs_hash: str | None = None) -> Manifest:
        """Record human approval for `stage_id`.

        If `current_outputs_hash` is supplied and differs from the stage's
        recorded `outputs_hash_at_done`, sets `manually_edited=True`. Raises
        `CannotApproveNonDoneStage` if the stage is not currently `done`.
        """
        record = self._require_stage(stage_id)
        if record.status != StageStatus.DONE:
            raise CannotApproveNonDoneStage(
                f"stage {stage_id!r} is {record.status.value!r}; only done stages may be approved"
            )
        manually_edited = record.manually_edited
        if current_outputs_hash is not None and record.outputs_hash_at_done is not None:
            manually_edited = current_outputs_hash != record.outputs_hash_at_done
        new_record = record.model_copy(
            update={"human_approved_at": _utcnow(), "manually_edited": manually_edited}
        )
        return self._with_stage(stage_id, new_record)

    def reset(
        self,
        stage_id: str,
        *,
        downstream_of: Mapping[str, Iterable[str]] | None = None,
    ) -> Manifest:
        """Return a new Manifest with `stage_id` and its transitive downstream reset.

        `downstream_of` maps each stage_id to the set of stage_ids that directly
        depend on it (upstream → downstream). If `None`, only `stage_id` is reset.

        A reset clears: status → PENDING, outputs, inputs_hash, outputs_hash_at_done,
        started_at, finished_at, human_approved_at, manually_edited, error, metrics,
        notes.
        """
        self._require_stage(stage_id)
        to_reset: set[str] = {stage_id}
        if downstream_of is not None:
            queue: list[str] = [stage_id]
            while queue:
                current = queue.pop()
                for downstream in downstream_of.get(current, ()):
                    if downstream not in to_reset and downstream in self.stages:
                        to_reset.add(downstream)
                        queue.append(downstream)
        new_stages = dict(self.stages)
        for sid in to_reset:
            new_stages[sid] = StageRecord(status=StageStatus.PENDING)
        return self.model_copy(update={"stages": new_stages, "updated_at": _utcnow()})

    def update_config_snapshot(self, new_config: dict[str, Any]) -> Manifest:
        """Replace `config_snapshot`. Only legal while every stage is still `pending`."""
        for stage_id, record in self.stages.items():
            if record.status != StageStatus.PENDING:
                raise ConfigSnapshotLocked(
                    f"cannot update config_snapshot: stage {stage_id!r} is {record.status.value!r}"
                )
        return self.model_copy(update={"config_snapshot": new_config, "updated_at": _utcnow()})

    # ------------------------------------------------------------------ helpers

    def _require_stage(self, stage_id: str) -> StageRecord:
        record = self.stages.get(stage_id)
        if record is None:
            raise UnknownStageId(f"stage_id {stage_id!r} is not in the manifest")
        return record

    def _with_stage(self, stage_id: str, record: StageRecord) -> Manifest:
        new_stages = {**self.stages, stage_id: record}
        return self.model_copy(update={"stages": new_stages, "updated_at": _utcnow()})


def _utcnow() -> datetime:
    """Return the current UTC time. Module-level so tests can monkeypatch."""
    return datetime.now(UTC)


def dump_json_canonical(data: object) -> str:
    """Serialize `data` with the canonical JSON rules used for all shipcast artifacts.

    Rules: `sort_keys=True`, `indent=2`, `ensure_ascii=False`,
    `separators=(",", ": ")`, trailing newline. Round-tripping artifact files
    serialized with this helper is byte-equivalent — required for stage
    idempotency tests.
    """
    return (
        json.dumps(
            data,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            separators=(",", ": "),
        )
        + "\n"
    )


# ---------------------------------------------------------------------- hashing


def compute_inputs_hash(paths: Iterable[Path]) -> str:
    """Fast SHA-256 over `(rel_path, mtime_ns, size_bytes)` triples.

    Used for `StageRecord.inputs_hash`. Two distinct change kinds — file
    appearance/disappearance, size change, mtime change — all invalidate the hash.
    Same-mtime same-size byte-level edits do NOT — `compute_outputs_hash` covers
    that case for the upstream stage's `outputs_hash_at_done`.

    Returns a fixed-length 64-character hex digest (full SHA-256, no truncation).
    """
    parts: list[tuple[str, str, int, int]] = []
    for path in sorted(paths, key=lambda p: str(p)):
        if not path.exists():
            parts.append((str(path), "missing", 0, 0))
            continue
        stat = path.stat()
        parts.append((str(path), "present", stat.st_mtime_ns, stat.st_size))
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert len(digest) == SHA256_HEX_LEN  # invariant: fixed-length SHA-256 output
    return digest


def compute_outputs_hash(paths: Iterable[Path]) -> str:
    """Byte-content SHA-256 over `(rel_path, file_sha256_hex)` pairs.

    Used for `StageRecord.outputs_hash_at_done` and `shipcast approve`'s
    recomputation. Detects ANY byte-level change in the listed files (including
    no-op-mtime touches that bump mtime but leave bytes identical, and
    coincidental same-size byte swaps).

    Returns a fixed-length 64-character hex digest. An empty `paths` argument
    yields the SHA-256 of the canonical empty-list payload (a stable sentinel
    distinct from any non-empty input).
    """
    parts: list[tuple[str, str]] = []
    for path in sorted(paths, key=lambda p: str(p)):
        if not path.exists():
            parts.append((str(path), "missing"))
            continue
        file_digest = _sha256_file(path)
        parts.append((str(path), file_digest))
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert len(digest) == SHA256_HEX_LEN  # invariant: fixed-length SHA-256 output
    return digest


def _sha256_file(path: Path, chunk_size: int = 64 * 1024) -> str:
    """Return the SHA-256 hex digest of a file's bytes, streamed in chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()
