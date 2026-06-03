"""A reusable in-test `FakeStage` for Slice-1 dispatcher/gate/rerun/reset tests.

`ALL_STAGES` is empty in Slice 1, so the dispatcher resolves stages from the
live registry (`shipcast.stages.ALL_STAGES`). Tests monkeypatch that tuple to
inject `FakeStage` (id `01_pick`) so the verb dispatches end-to-end.

Not a test module (underscore prefix) — imported by test modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from shipcast.project import Project
from shipcast.stage import StageResult, StageStatus
from shipcast.stages import BaseStage


class PickOutput(BaseModel):
    """Trivial output schema for the fake pick stage."""

    ok: bool


class FakeStage(BaseStage):
    """A minimal concrete stage: writes one JSON output and returns DONE."""

    id: ClassVar[str] = "01_pick"
    requires: ClassVar[tuple[str, ...]] = ()
    output_schema: ClassVar[type[BaseModel]] = PickOutput
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Confirm the picked changelog entry matches the requested heading.",
        "Verify entry.json contains a non-empty summary.",
        "Check the date/time fields parse as UTC.",
    )

    def run(self, project: Project) -> StageResult:
        out_dir = project.stage_dir(self.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "entry.json"
        out_file.write_text('{\n  "ok": true\n}\n', encoding="utf-8")
        rel = out_file.relative_to(project.path)
        return StageResult(status=StageStatus.DONE, outputs=(rel,))


class DownstreamStage(BaseStage):
    """A second stage requiring `01_pick`, used to exercise cascade/reset."""

    id: ClassVar[str] = "02_enrich"
    requires: ClassVar[tuple[str, ...]] = ("01_pick",)
    output_schema: ClassVar[type[BaseModel]] = PickOutput
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Confirm the enrichment narrative is non-empty.",
    )

    def run(self, project: Project) -> StageResult:
        out_dir = project.stage_dir(self.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "context.json"
        out_file.write_text('{\n  "ok": true\n}\n', encoding="utf-8")
        rel = out_file.relative_to(project.path)
        return StageResult(status=StageStatus.DONE, outputs=(rel,))


def output_path(project: Project) -> Path:
    """Absolute path to FakeStage's declared output file."""
    return project.stage_dir(FakeStage.id) / "entry.json"
