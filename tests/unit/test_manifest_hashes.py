"""Coverage-completing unit tests for `shipcast.manifest`.

This module drives `shipcast.manifest` to 100% line AND branch coverage,
covering both hash families, the round-trip serialization contract, and every
error / edge branch in the data model that the Slice-1 happy-path tests in
`test_manifest.py` do not reach.

Owned TCs (Slice 4):
- TC-1.4  manifest round-trip byte-identical to the pinned fixture
- TC-1.5  `compute_inputs_hash` stable — same inputs → same digest
- TC-1.6  `compute_inputs_hash` sensitive to mtime change
- TC-1.7  `compute_inputs_hash` sensitive to size change
- TC-1.8  `compute_outputs_hash` detects same-size byte swap (inputs_hash would NOT)
- TC-1.9  `compute_outputs_hash` ignores mtime-only changes (byte-content hash)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shipcast.errors import (
    ManifestCorrupt,
    UnknownStageId,
)
from shipcast.manifest import (
    SHA256_HEX_LEN,
    Manifest,
    StageRecord,
    StageStatus,
    compute_inputs_hash,
    compute_outputs_hash,
)

_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "manifests"
    / "v1_fresh.json"
)


def _manifest(stages: dict[str, StageRecord] | None = None) -> Manifest:
    return Manifest(
        slug="entry",
        created_at=_EPOCH,
        updated_at=_EPOCH,
        config_snapshot={},
        stages=stages or {"s": StageRecord()},
    )


# --------------------------------------------------------------------------- #
# TC-1.4 — round-trip byte-equality against the pinned fixture
# --------------------------------------------------------------------------- #


def test_tc_1_4_round_trip_byte_identical_to_fixture(tmp_path: Path) -> None:
    """TC-1.4: load the pinned fixture → save → bytes match the fixture exactly."""
    loaded = Manifest.load(FIXTURE)
    out = tmp_path / "manifest.json"
    loaded.save(out)
    assert out.read_bytes() == FIXTURE.read_bytes()


def test_tc_1_4_fixture_is_canonical_serialization() -> None:
    """The pinned fixture is byte-identical to the canonical serializer output.

    Guards against the fixture drifting away from `Manifest.serialize()`'s
    canonical form (sorted keys, indent=2, trailing newline).
    """
    loaded = Manifest.load(FIXTURE)
    assert loaded.serialize() == FIXTURE.read_text(encoding="utf-8")
    assert FIXTURE.read_text(encoding="utf-8").endswith("\n")


# --------------------------------------------------------------------------- #
# TC-1.5 / TC-1.6 / TC-1.7 — compute_inputs_hash stability + sensitivity
# --------------------------------------------------------------------------- #


def test_tc_1_5_inputs_hash_stable_same_inputs(tmp_path: Path) -> None:
    """TC-1.5: identical inputs → identical 64-char digest, no non-determinism."""
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha", encoding="utf-8")
    p2.write_text("beta", encoding="utf-8")
    h1 = compute_inputs_hash([p1, p2])
    h2 = compute_inputs_hash([p1, p2])
    assert h1 == h2
    assert len(h1) == SHA256_HEX_LEN


def test_tc_1_5_inputs_hash_order_independent(tmp_path: Path) -> None:
    """TC-1.5: the input list order does not affect the digest (sorted internally)."""
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha", encoding="utf-8")
    p2.write_text("beta", encoding="utf-8")
    assert compute_inputs_hash([p1, p2]) == compute_inputs_hash([p2, p1])


def test_tc_1_6_inputs_hash_sensitive_to_mtime(tmp_path: Path) -> None:
    """TC-1.6: bumping a file's mtime changes the inputs hash."""
    p = tmp_path / "a.txt"
    p.write_text("alpha", encoding="utf-8")
    h1 = compute_inputs_hash([p])
    stat = p.stat()
    new_t = stat.st_mtime + 1
    os.utime(p, (new_t, new_t))
    h2 = compute_inputs_hash([p])
    assert h1 != h2


def test_tc_1_7_inputs_hash_sensitive_to_size(tmp_path: Path) -> None:
    """TC-1.7: changing a file's size changes the inputs hash."""
    p = tmp_path / "a.txt"
    p.write_text("alpha", encoding="utf-8")
    h1 = compute_inputs_hash([p])
    with p.open("ab") as f:
        f.write(b"x")
    h2 = compute_inputs_hash([p])
    assert h1 != h2


def test_inputs_hash_missing_path_branch(tmp_path: Path) -> None:
    """compute_inputs_hash records a 'missing' sentinel for absent paths.

    Covers the `if not path.exists()` branch and proves a missing file yields a
    distinct, stable digest from a present one.
    """
    present = tmp_path / "present.txt"
    present.write_text("x", encoding="utf-8")
    missing = tmp_path / "missing.txt"
    h_missing = compute_inputs_hash([missing])
    assert len(h_missing) == SHA256_HEX_LEN
    # Stable across calls while still absent.
    assert h_missing == compute_inputs_hash([missing])
    # A present file at the same path produces a different digest.
    missing.write_text("now here", encoding="utf-8")
    assert compute_inputs_hash([missing]) != h_missing


# --------------------------------------------------------------------------- #
# TC-1.8 / TC-1.9 — compute_outputs_hash byte-content semantics
# --------------------------------------------------------------------------- #


def test_tc_1_8_outputs_hash_detects_same_size_byte_swap(tmp_path: Path) -> None:
    """TC-1.8: a same-size byte swap with preserved mtime changes outputs hash.

    Negative assertion: `compute_inputs_hash` (mtime+size) does NOT detect the
    swap — the two hash families diverge on this exact input, which is the
    load-bearing asymmetry.
    """
    p = tmp_path / "a.bin"
    p.write_bytes(b"A" * 100)
    stat = p.stat()
    out_h1 = compute_outputs_hash([p])
    in_h1 = compute_inputs_hash([p])

    p.write_bytes(b"B" * 100)  # same size, different bytes
    # Preserve mtime at NANOSECOND precision — compute_inputs_hash keys on
    # `st_mtime_ns`, so a float-rounded utime would not round-trip exactly.
    os.utime(p, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    out_h2 = compute_outputs_hash([p])
    in_h2 = compute_inputs_hash([p])

    # outputs hash (byte content) sees the swap...
    assert out_h1 != out_h2
    # ...inputs hash (mtime+size) does NOT.
    assert in_h1 == in_h2


def test_tc_1_9_outputs_hash_ignores_mtime_only_change(tmp_path: Path) -> None:
    """TC-1.9: byte-identical replacement with a bumped mtime → same outputs hash."""
    p = tmp_path / "a.txt"
    p.write_text("hello world", encoding="utf-8")
    h1 = compute_outputs_hash([p])
    p.write_text("hello world", encoding="utf-8")  # identical bytes
    new_t = p.stat().st_mtime + 5
    os.utime(p, (new_t, new_t))
    h2 = compute_outputs_hash([p])
    assert h1 == h2


def test_outputs_hash_missing_path_branch(tmp_path: Path) -> None:
    """compute_outputs_hash records a 'missing' sentinel for absent paths."""
    missing = tmp_path / "missing.txt"
    h_missing = compute_outputs_hash([missing])
    assert len(h_missing) == SHA256_HEX_LEN
    assert h_missing == compute_outputs_hash([missing])
    missing.write_text("content", encoding="utf-8")
    assert compute_outputs_hash([missing]) != h_missing


def test_outputs_hash_empty_paths_is_stable_sentinel() -> None:
    """compute_outputs_hash([]) is a stable digest distinct from any non-empty input."""
    h_empty = compute_outputs_hash([])
    assert len(h_empty) == SHA256_HEX_LEN
    assert h_empty == compute_outputs_hash([])


# --------------------------------------------------------------------------- #
# Manifest.load error branches (135-136, 139-140, 142, 150-151)
# --------------------------------------------------------------------------- #


def test_load_unreadable_path_raises_manifest_corrupt(tmp_path: Path) -> None:
    """OSError while reading (path is a directory) → ManifestCorrupt."""
    a_dir = tmp_path / "amanifest"
    a_dir.mkdir()
    with pytest.raises(ManifestCorrupt, match="cannot read manifest"):
        Manifest.load(a_dir)


def test_load_invalid_json_raises_manifest_corrupt(tmp_path: Path) -> None:
    """Non-JSON bytes → ManifestCorrupt mentioning invalid JSON."""
    p = tmp_path / "manifest.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ManifestCorrupt, match="not valid JSON"):
        Manifest.load(p)


def test_load_non_object_json_raises_manifest_corrupt(tmp_path: Path) -> None:
    """A JSON array (not an object) → ManifestCorrupt."""
    p = tmp_path / "manifest.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ManifestCorrupt, match="not a JSON object"):
        Manifest.load(p)


def test_load_schema_violation_raises_manifest_corrupt(tmp_path: Path) -> None:
    """Right schema_version but a field-type violation → ManifestCorrupt (ValidationError)."""
    p = tmp_path / "manifest.json"
    # schema_version is correct so we get past the migration gate, but `slug`
    # is the wrong type, tripping pydantic validation.
    p.write_text(
        '{"schema_version": 1, "slug": 123, "created_at": "2000-01-01T00:00:00Z", '
        '"updated_at": "2000-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    with pytest.raises(ManifestCorrupt, match="failed validation"):
        Manifest.load(p)


# --------------------------------------------------------------------------- #
# approve — hash-None short-circuit branch (202->204)
# --------------------------------------------------------------------------- #


def test_approve_without_current_hash_leaves_manually_edited_false() -> None:
    """approve(current_outputs_hash=None) skips the mismatch check (default branch)."""
    m = _manifest(
        {"s": StageRecord(status=StageStatus.DONE, outputs_hash_at_done="a" * 64)}
    )
    out = m.approve("s")  # no current hash supplied
    assert out.stages["s"].human_approved_at is not None
    assert out.stages["s"].manually_edited is False


def test_approve_with_hash_but_no_stored_hash_skips_check() -> None:
    """approve with a current hash but stored hash None → mismatch check skipped.

    Covers the right-hand side of the short-circuit (`record.outputs_hash_at_done
    is not None`): when nothing was recorded at done-time there is nothing to
    diff against, so manually_edited stays at its prior value.
    """
    m = _manifest(
        {"s": StageRecord(status=StageStatus.DONE, outputs_hash_at_done=None)}
    )
    out = m.approve("s", current_outputs_hash="a" * 64)
    assert out.stages["s"].human_approved_at is not None
    assert out.stages["s"].manually_edited is False


# --------------------------------------------------------------------------- #
# reset cascade — already-seen / not-in-stages skip branch (231->230)
# --------------------------------------------------------------------------- #


def test_reset_cascade_handles_already_visited_and_unknown_downstream() -> None:
    """Diamond + dangling-id downstream graph exercises the dedupe/skip branch.

    `a` fans out to `b` and `c`, both of which fan into `d` (diamond — `d` is
    reached twice, so the `downstream not in to_reset` guard skips the second
    visit). `c` also lists `ghost`, a stage id NOT in the manifest, exercising
    the `downstream in self.stages` guard.
    """
    stages = {
        sid: StageRecord(status=StageStatus.DONE) for sid in ("a", "b", "c", "d")
    }
    m = _manifest(stages)
    downstream = {
        "a": ["b", "c"],
        "b": ["d"],
        "c": ["d", "ghost"],  # "d" already queued; "ghost" not in manifest
        "d": [],
    }
    out = m.reset("a", downstream_of=downstream)
    for sid in ("a", "b", "c", "d"):
        assert out.stages[sid].status == StageStatus.PENDING
    assert "ghost" not in out.stages


# --------------------------------------------------------------------------- #
# _require_stage — UnknownStageId (line 253)
# --------------------------------------------------------------------------- #


def test_transition_unknown_stage_raises() -> None:
    """transition on an unknown stage id → UnknownStageId."""
    m = _manifest()
    with pytest.raises(UnknownStageId):
        m.transition("nope", StageStatus.RUNNING)


def test_approve_unknown_stage_raises() -> None:
    """approve on an unknown stage id → UnknownStageId."""
    m = _manifest()
    with pytest.raises(UnknownStageId):
        m.approve("nope")


def test_reset_unknown_stage_raises() -> None:
    """reset on an unknown stage id → UnknownStageId."""
    m = _manifest()
    with pytest.raises(UnknownStageId):
        m.reset("nope")
