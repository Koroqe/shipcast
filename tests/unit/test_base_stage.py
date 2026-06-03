"""Unit tests for BaseStage default behavior — covers lines 101,133,137,143,148,163,165,170-175,217,234,246-248.

Uses a minimal FakeStage / FakeProject to exercise:
- check_inputs: missing upstream, not-done upstream, not-approved upstream, missing artifact
- validate_outputs: empty outputs (no-op), single output schema validation, absolute path rejection,
  path-escape rejection, missing file rejection
- additional_input_paths: default returns empty iterable
- pre_run_hook: default is a no-op
- run(): raises NotImplementedError on base class
- upstream_artifact_paths: returns correct absolute paths
- compute_stage_inputs_hash: incorporates upstream paths + additional
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from shipcast.errors import StageInputMissing, StageNotApproved, StageOutputInvalid
from shipcast.manifest import StageStatus
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

# ---------------------------------------------------------------------------
# Minimal schema used in single-output validate_outputs tests
# ---------------------------------------------------------------------------


class FakeOutput(BaseModel):
    ok: bool


# ---------------------------------------------------------------------------
# Concrete test stage — minimal wiring
# ---------------------------------------------------------------------------


class FakeTestStage(BaseStage):
    id: ClassVar[str] = "02_enrich"
    requires: ClassVar[tuple[str, ...]] = ("01_pick",)
    output_schema: ClassVar[type[BaseModel]] = FakeOutput
    review_checklist_items: ClassVar[tuple[str, ...]] = ("Check it.",)

    def run(self, project: Any) -> StageResult:  # type: ignore[override]
        return StageResult(status=StageStatus.DONE, outputs=())


class NoRequiresStage(BaseStage):
    """Stage with no upstream dependencies."""

    id: ClassVar[str] = "01_pick"
    requires: ClassVar[tuple[str, ...]] = ()
    output_schema: ClassVar[type[BaseModel]] = FakeOutput
    review_checklist_items: ClassVar[tuple[str, ...]] = ("ok",)

    def run(self, project: Any) -> StageResult:  # type: ignore[override]
        return StageResult(status=StageStatus.DONE, outputs=())


# ---------------------------------------------------------------------------
# Minimal project double
# ---------------------------------------------------------------------------


class _FakeManifest:
    def __init__(self, stages: dict[str, Any]) -> None:
        self.stages = stages


class _FakeProject:
    def __init__(self, root: Path, stages: dict[str, Any]) -> None:
        self.path = root
        self.manifest = _FakeManifest(stages)

    def stage_dir(self, stage_id: str) -> Path:
        return self.path / stage_id

    def artifact_path(self, stage_id: str, name: str) -> Path:
        return self.stage_dir(stage_id) / name


def _approved_stage_record(outputs: tuple[str, ...] = ()) -> Any:
    """A StageRecord-like object that is DONE and human-approved."""
    _outputs = outputs

    class _Rec:
        status = StageStatus.DONE
        human_approved_at: Any = datetime(2024, 1, 1, tzinfo=UTC)
        outputs = _outputs

    return _Rec()


def _done_not_approved_record(outputs: tuple[str, ...] = ()) -> Any:
    """DONE but not approved."""
    _outputs = outputs

    class _Rec:
        status = StageStatus.DONE
        human_approved_at = None
        outputs = _outputs

    return _Rec()


def _pending_record() -> Any:
    """Still pending (not done)."""

    class _Rec:
        status = StageStatus.PENDING
        human_approved_at = None
        outputs: tuple[str, ...] = ()

    return _Rec()


# ---------------------------------------------------------------------------
# check_inputs tests
# ---------------------------------------------------------------------------


def test_check_inputs_missing_upstream_raises_stage_input_missing(
    tmp_path: Path,
) -> None:
    """Upstream stage missing from manifest → StageInputMissing."""
    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    with pytest.raises(StageInputMissing, match="01_pick"):
        stage.check_inputs(project)  # type: ignore[arg-type]


def test_check_inputs_upstream_not_done_raises_stage_input_missing(
    tmp_path: Path,
) -> None:
    """Upstream stage present but not done → StageInputMissing."""
    project = _FakeProject(tmp_path, stages={"01_pick": _pending_record()})
    stage = FakeTestStage()
    with pytest.raises(StageInputMissing, match="01_pick"):
        stage.check_inputs(project)  # type: ignore[arg-type]


def test_check_inputs_upstream_not_approved_raises_stage_not_approved(
    tmp_path: Path,
) -> None:
    """Upstream stage done but not approved → StageNotApproved."""
    project = _FakeProject(tmp_path, stages={"01_pick": _done_not_approved_record()})
    stage = FakeTestStage()
    with pytest.raises(StageNotApproved, match="01_pick"):
        stage.check_inputs(project)  # type: ignore[arg-type]


def test_check_inputs_missing_artifact_on_disk_raises_stage_input_missing(
    tmp_path: Path,
) -> None:
    """Upstream stage approved but its declared output file is absent → StageInputMissing."""
    # The record declares "01_pick/entry.json" but we don't create the file
    record = _approved_stage_record(outputs=("01_pick/entry.json",))
    project = _FakeProject(tmp_path, stages={"01_pick": record})
    stage = FakeTestStage()
    with pytest.raises(StageInputMissing, match=r"entry\.json"):
        stage.check_inputs(project)  # type: ignore[arg-type]


def test_check_inputs_all_good_no_raise(tmp_path: Path) -> None:
    """Upstream stage approved + artifact on disk → check_inputs passes silently."""
    artifact = tmp_path / "01_pick" / "entry.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"ok": true}', encoding="utf-8")
    record = _approved_stage_record(outputs=("01_pick/entry.json",))
    project = _FakeProject(tmp_path, stages={"01_pick": record})
    stage = FakeTestStage()
    stage.check_inputs(project)  # type: ignore[arg-type]  # must not raise


def test_check_inputs_no_requires_always_passes(tmp_path: Path) -> None:
    """Stage with no requires passes check_inputs even with empty manifest."""
    project = _FakeProject(tmp_path, stages={})
    stage = NoRequiresStage()
    stage.check_inputs(project)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_outputs tests
# ---------------------------------------------------------------------------


def test_validate_outputs_empty_outputs_is_noop(tmp_path: Path) -> None:
    """Empty outputs tuple → validate_outputs is a no-op (no error)."""
    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    result = StageResult(status=StageStatus.DONE, outputs=())
    stage.validate_outputs(project, result)  # type: ignore[arg-type]


def test_validate_outputs_single_valid_json(tmp_path: Path) -> None:
    """Single output that validates against output_schema → no error."""
    stage_dir = tmp_path / "02_enrich"
    stage_dir.mkdir(parents=True)
    out_file = stage_dir / "context.json"
    out_file.write_text('{"ok": true}', encoding="utf-8")

    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    rel = Path("02_enrich/context.json")
    result = StageResult(status=StageStatus.DONE, outputs=(rel,))
    stage.validate_outputs(project, result)  # type: ignore[arg-type]


def test_validate_outputs_single_invalid_json_schema_raises(tmp_path: Path) -> None:
    """Single output that fails Pydantic schema → StageOutputInvalid."""
    stage_dir = tmp_path / "02_enrich"
    stage_dir.mkdir(parents=True)
    out_file = stage_dir / "context.json"
    # Schema expects {"ok": bool}; this has wrong type
    out_file.write_text('{"ok": "not-a-bool"}', encoding="utf-8")

    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    rel = Path("02_enrich/context.json")
    result = StageResult(status=StageStatus.DONE, outputs=(rel,))
    with pytest.raises(StageOutputInvalid):
        stage.validate_outputs(project, result)  # type: ignore[arg-type]


def test_validate_outputs_single_malformed_json_raises(tmp_path: Path) -> None:
    """Single output with invalid JSON → StageOutputInvalid."""
    stage_dir = tmp_path / "02_enrich"
    stage_dir.mkdir(parents=True)
    (stage_dir / "context.json").write_text("NOT JSON {{{", encoding="utf-8")

    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    rel = Path("02_enrich/context.json")
    result = StageResult(status=StageStatus.DONE, outputs=(rel,))
    with pytest.raises(StageOutputInvalid, match="not valid JSON"):
        stage.validate_outputs(project, result)  # type: ignore[arg-type]


def test_validate_outputs_absolute_path_raises(tmp_path: Path) -> None:
    """Absolute path in outputs → StageOutputInvalid (defense-in-depth)."""
    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    abs_path = tmp_path / "02_enrich" / "context.json"
    abs_path.parent.mkdir(parents=True)
    abs_path.write_text('{"ok": true}', encoding="utf-8")
    result = StageResult(status=StageStatus.DONE, outputs=(abs_path,))
    with pytest.raises(StageOutputInvalid, match="absolute"):
        stage.validate_outputs(project, result)  # type: ignore[arg-type]


def test_validate_outputs_path_escape_raises(tmp_path: Path) -> None:
    """Path escaping stage_dir via .. → StageOutputInvalid."""
    # Create the "escaped" file one level above stage_dir so it actually exists
    # but resolves outside 02_enrich/
    escaped_file = tmp_path / "secrets.json"
    escaped_file.write_text('{"ok": true}', encoding="utf-8")
    (tmp_path / "02_enrich").mkdir(parents=True, exist_ok=True)

    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    # Relative path that escapes the stage_dir
    rel = Path("02_enrich/../secrets.json")
    result = StageResult(status=StageStatus.DONE, outputs=(rel,))
    with pytest.raises(StageOutputInvalid):
        stage.validate_outputs(project, result)  # type: ignore[arg-type]


def test_validate_outputs_missing_file_raises(tmp_path: Path) -> None:
    """Declared output file absent → StageOutputInvalid."""
    (tmp_path / "02_enrich").mkdir(parents=True, exist_ok=True)
    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    rel = Path("02_enrich/missing.json")
    result = StageResult(status=StageStatus.DONE, outputs=(rel,))
    with pytest.raises(StageOutputInvalid, match="missing"):
        stage.validate_outputs(project, result)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# additional_input_paths default
# ---------------------------------------------------------------------------


def test_additional_input_paths_default_returns_empty(tmp_path: Path) -> None:
    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    result = list(stage.additional_input_paths(project))  # type: ignore[arg-type]
    assert result == []


# ---------------------------------------------------------------------------
# pre_run_hook default (no-op)
# ---------------------------------------------------------------------------


def test_pre_run_hook_default_is_noop(tmp_path: Path) -> None:
    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    # Should not raise
    stage.pre_run_hook(project)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run() on bare BaseStage raises NotImplementedError
# ---------------------------------------------------------------------------


def test_base_stage_run_raises_not_implemented(tmp_path: Path) -> None:
    project = _FakeProject(tmp_path, stages={})

    class _Bare(BaseStage):
        id: ClassVar[str] = "99_bare"
        requires: ClassVar[tuple[str, ...]] = ()
        output_schema: ClassVar[type[BaseModel]] = FakeOutput
        review_checklist_items: ClassVar[tuple[str, ...]] = ()

    with pytest.raises(NotImplementedError):
        _Bare().run(project)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# upstream_artifact_paths
# ---------------------------------------------------------------------------


def test_upstream_artifact_paths_returns_absolute(tmp_path: Path) -> None:
    record = _approved_stage_record(outputs=("01_pick/entry.json", "01_pick/meta.json"))
    project = _FakeProject(tmp_path, stages={"01_pick": record})
    stage = FakeTestStage()
    paths = stage.upstream_artifact_paths(project)  # type: ignore[arg-type]
    assert len(paths) == 2
    for p in paths:
        assert p.is_absolute()
    assert any("entry.json" in str(p) for p in paths)


def test_upstream_artifact_paths_skips_missing_upstream(tmp_path: Path) -> None:
    """If upstream is not in the manifest, upstream_artifact_paths silently skips it."""
    project = _FakeProject(tmp_path, stages={})
    stage = FakeTestStage()
    paths = stage.upstream_artifact_paths(project)  # type: ignore[arg-type]
    assert paths == []


# ---------------------------------------------------------------------------
# compute_stage_inputs_hash incorporates additional_input_paths
# ---------------------------------------------------------------------------


def test_compute_stage_inputs_hash_changes_with_additional_path(
    tmp_path: Path,
) -> None:
    """Two calls with different additional_input_paths content produce different hashes."""
    # We need a real-ish project for the hash computation — use the record with a real file
    artifact = tmp_path / "01_pick" / "entry.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"ok": true}', encoding="utf-8")
    record = _approved_stage_record(outputs=("01_pick/entry.json",))
    project = _FakeProject(tmp_path, stages={"01_pick": record})

    extra_file = tmp_path / "extra.txt"
    extra_file.write_text("v1", encoding="utf-8")

    class _WithExtra(FakeTestStage):
        def additional_input_paths(self, project: Any) -> list[Path]:  # type: ignore[override]
            return [extra_file]

    class _WithoutExtra(FakeTestStage):
        pass

    h_with = _WithExtra().compute_stage_inputs_hash(project)  # type: ignore[arg-type]
    h_without = _WithoutExtra().compute_stage_inputs_hash(project)  # type: ignore[arg-type]
    assert isinstance(h_with, str)
    assert isinstance(h_without, str)
    # The extra file's mtime+size appears in one but not the other
    assert h_with != h_without


def test_compute_stage_inputs_hash_returns_string(tmp_path: Path) -> None:
    project = _FakeProject(tmp_path, stages={})
    stage = NoRequiresStage()
    h = stage.compute_stage_inputs_hash(project)  # type: ignore[arg-type]
    assert isinstance(h, str)
    assert len(h) > 0
