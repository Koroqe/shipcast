"""Unit tests for `shipcast.manifest` — state transitions, hashing, atomic save.

Owned TCs (Slice 1):
- TC-1.1  legal state transitions (exhaustive parametrized happy paths)
- TC-1.2  illegal state transitions raise IllegalTransition
- TC-1.3  atomic write — mid-write crash leaves original intact, .tmp present
- TC-1.10 config_snapshot locked after first stage leaves pending
- TC-1.11 config_snapshot writable when all stages pending
- TC-1.12 ManifestMigrationNeeded on schema_version mismatch
- TC-1.13 reset clears stage fields and resets downstream transitively
- TC-1.14 approve sets human_approved_at, manually_edited stays false on hash match
- TC-1.15 approve sets manually_edited=true on hash mismatch
- TC-1.16 CannotApproveNonDoneStage when stage not done
- TC-1.17 reset/transition on a truly-running stage (running→running illegal)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shipcast.errors import (
    CannotApproveNonDoneStage,
    ConfigSnapshotLocked,
    IllegalTransition,
    ManifestMigrationNeeded,
)
from shipcast.manifest import (
    _LEGAL_TRANSITIONS,
    CURRENT_SCHEMA_VERSION,
    Manifest,
    StageRecord,
    StageStatus,
    compute_outputs_hash,
    is_legal_transition,
)

_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

_ALL_STATUSES = list(StageStatus)


def _manifest(stages: dict[str, StageRecord] | None = None) -> Manifest:
    return Manifest(
        slug="entry",
        created_at=_EPOCH,
        updated_at=_EPOCH,
        config_snapshot={},
        stages=stages or {"s": StageRecord()},
    )


# --------------------------------------------------------------------------- #
# TC-1.1 — legal transitions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("frm", "to"), sorted(_LEGAL_TRANSITIONS))
def test_tc_1_1_legal_transitions(frm: StageStatus, to: StageStatus) -> None:
    """TC-1.1: every pair in the legal matrix transitions without raising."""
    m = _manifest({"s": StageRecord(status=frm)})
    out = m.transition("s", to)
    assert out.stages["s"].status == to
    assert is_legal_transition(frm, to)


def test_tc_1_1_exactly_eight_legal_transitions() -> None:
    """TC-1.1: the legal matrix has exactly 8 entries (architecture contract)."""
    assert len(_LEGAL_TRANSITIONS) == 8


# --------------------------------------------------------------------------- #
# TC-1.2 / TC-1.17 — illegal transitions (incl. running→running, done→done)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("frm", _ALL_STATUSES)
@pytest.mark.parametrize("to", _ALL_STATUSES)
def test_tc_1_2_illegal_transitions_raise(frm: StageStatus, to: StageStatus) -> None:
    """TC-1.2/TC-1.17: every pair NOT in the legal list raises IllegalTransition.

    Includes same-state no-ops like done→done and the running→running case
    (TC-1.17 / UC-32): a truly-running stage cannot re-enter running.
    """
    if (frm, to) in _LEGAL_TRANSITIONS:
        pytest.skip("legal transition covered by TC-1.1")
    m = _manifest({"s": StageRecord(status=frm)})
    with pytest.raises(IllegalTransition):
        m.transition("s", to)


def test_tc_1_17_running_to_running_is_illegal() -> None:
    """TC-1.17: running→running is explicitly illegal (no double-start)."""
    m = _manifest({"s": StageRecord(status=StageStatus.RUNNING)})
    with pytest.raises(IllegalTransition):
        m.transition("s", StageStatus.RUNNING)


# --------------------------------------------------------------------------- #
# TC-1.3 — atomic write crash safety
# --------------------------------------------------------------------------- #


def test_tc_1_3_atomic_write_crash_leaves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-1.3: if os.replace raises mid-write, manifest.json is byte-unchanged and .tmp exists."""
    path = tmp_path / "manifest.json"
    original = _manifest()
    original.save(path)
    original_bytes = path.read_bytes()

    edited = original.transition("s", StageStatus.RUNNING)

    def _boom(src: object, dst: object) -> None:
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated crash"):
        edited.save(path)

    # Original manifest is byte-for-byte intact.
    assert path.read_bytes() == original_bytes
    # The .tmp file is left behind (proof the write happened before the rename).
    assert (tmp_path / "manifest.json.tmp").is_file()


def test_round_trip_byte_identical(tmp_path: Path) -> None:
    """Manifest save → load → serialize is byte-identical (idempotency)."""
    path = tmp_path / "manifest.json"
    m = _manifest()
    m.save(path)
    reloaded = Manifest.load(path)
    assert reloaded.serialize() == path.read_text(encoding="utf-8")
    assert reloaded.serialize().endswith("\n")


# --------------------------------------------------------------------------- #
# TC-1.10 / TC-1.11 — config_snapshot lock
# --------------------------------------------------------------------------- #


def test_tc_1_11_config_snapshot_writable_when_all_pending() -> None:
    """TC-1.11: config_snapshot can be replaced while every stage is pending."""
    m = _manifest({"a": StageRecord(), "b": StageRecord()})
    out = m.update_config_snapshot({"voice_id": "abc"})
    assert out.config_snapshot == {"voice_id": "abc"}


def test_tc_1_10_config_snapshot_locked_after_stage_leaves_pending() -> None:
    """TC-1.10: once any stage leaves pending, config_snapshot is locked."""
    m = _manifest({"a": StageRecord(status=StageStatus.RUNNING), "b": StageRecord()})
    with pytest.raises(ConfigSnapshotLocked):
        m.update_config_snapshot({"voice_id": "abc"})


# --------------------------------------------------------------------------- #
# TC-1.12 — schema version mismatch
# --------------------------------------------------------------------------- #


def test_tc_1_12_migration_needed_on_version_mismatch(tmp_path: Path) -> None:
    """TC-1.12: loading a manifest with a wrong schema_version raises ManifestMigrationNeeded."""
    path = tmp_path / "manifest.json"
    m = _manifest()
    m.save(path)
    # Corrupt the version in place.
    text = path.read_text(encoding="utf-8").replace(
        f'"schema_version": {CURRENT_SCHEMA_VERSION}', '"schema_version": 999'
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ManifestMigrationNeeded):
        Manifest.load(path)


# --------------------------------------------------------------------------- #
# TC-1.13 — reset clears fields + cascades transitively
# --------------------------------------------------------------------------- #


def test_tc_1_13_reset_clears_fields_and_cascades() -> None:
    """TC-1.13: reset clears the stage and every transitive downstream stage."""
    stages = {
        "a": StageRecord(
            status=StageStatus.DONE,
            outputs=("a/out.json",),
            human_approved_at=_EPOCH,
            manually_edited=True,
            inputs_hash="x" * 64,
            outputs_hash_at_done="y" * 64,
        ),
        "b": StageRecord(status=StageStatus.DONE, human_approved_at=_EPOCH),
        "c": StageRecord(status=StageStatus.DONE, human_approved_at=_EPOCH),
    }
    m = _manifest(stages)
    # a → b → c chain.
    downstream = {"a": ["b"], "b": ["c"], "c": []}
    out = m.reset("a", downstream_of=downstream)
    for sid in ("a", "b", "c"):
        rec = out.stages[sid]
        assert rec.status == StageStatus.PENDING
        assert rec.outputs == ()
        assert rec.human_approved_at is None
        assert rec.manually_edited is False
        assert rec.inputs_hash is None
        assert rec.outputs_hash_at_done is None


def test_reset_without_downstream_resets_only_target() -> None:
    """reset with downstream_of=None resets only the named stage (degenerate case)."""
    stages = {
        "a": StageRecord(status=StageStatus.DONE),
        "b": StageRecord(status=StageStatus.DONE),
    }
    m = _manifest(stages)
    out = m.reset("a")
    assert out.stages["a"].status == StageStatus.PENDING
    assert out.stages["b"].status == StageStatus.DONE


# --------------------------------------------------------------------------- #
# TC-1.14 / TC-1.15 / TC-1.16 — approve + manual-edit detection
# --------------------------------------------------------------------------- #


def test_tc_1_14_approve_sets_timestamp_no_edit_on_hash_match() -> None:
    """TC-1.14: approving a done stage with matching hash → approved, manually_edited stays false."""
    stored = "a" * 64
    m = _manifest(
        {"s": StageRecord(status=StageStatus.DONE, outputs_hash_at_done=stored)}
    )
    out = m.approve("s", current_outputs_hash=stored)
    assert out.stages["s"].human_approved_at is not None
    assert out.stages["s"].manually_edited is False


def test_tc_1_15_approve_sets_manually_edited_on_hash_mismatch() -> None:
    """TC-1.15: approving with a differing recomputed hash → manually_edited=true."""
    m = _manifest(
        {"s": StageRecord(status=StageStatus.DONE, outputs_hash_at_done="a" * 64)}
    )
    out = m.approve("s", current_outputs_hash="b" * 64)
    assert out.stages["s"].human_approved_at is not None
    assert out.stages["s"].manually_edited is True


def test_tc_1_16_cannot_approve_non_done_stage() -> None:
    """TC-1.16: approving a stage that is not done raises CannotApproveNonDoneStage."""
    m = _manifest({"s": StageRecord(status=StageStatus.RUNNING)})
    with pytest.raises(CannotApproveNonDoneStage):
        m.approve("s")


# --------------------------------------------------------------------------- #
# outputs-hash sanity (supports approve edit-detection)
# --------------------------------------------------------------------------- #


def test_outputs_hash_detects_byte_change(tmp_path: Path) -> None:
    """compute_outputs_hash changes when a same-length file's bytes change."""
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    h1 = compute_outputs_hash([f])
    f.write_text("world", encoding="utf-8")  # same length, different bytes
    h2 = compute_outputs_hash([f])
    assert h1 != h2
