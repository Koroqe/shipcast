"""Stage Protocol, StageResult dataclass, and re-exported StageStatus.

A `Stage` is a single, human-gated step in the shipcast pipeline. Stages are
PURE in the sense that they:

* Read inputs from disk via `Project` helpers.
* Compute outputs and write them to disk inside their assigned `stage_dir`.
* Return a `StageResult` describing what they did.

Stages NEVER mutate the manifest directly — the CLI dispatcher (Slice 6) owns
that. Stages NEVER instantiate external API clients at module/import time;
clients are constructed lazily inside `run()` only.

`BaseStage` (in `shipcast.stages._base`) provides default implementations of
`check_inputs`, `validate_outputs`, `upstream_artifact_paths`,
`additional_input_paths`, and `pre_run_hook` so concrete stages only need to
declare `id`, `requires`, `output_schema`, `review_checklist_items`, and `run`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from shipcast.manifest import ErrorRecord, StageStatus

if TYPE_CHECKING:
    from shipcast.project import Project

__all__ = [
    "ErrorRecord",
    "Stage",
    "StageResult",
    "StageStatus",
]


@dataclass(frozen=True)
class StageResult:
    """The outcome of a single `Stage.run()` invocation.

    `outputs` are paths RELATIVE to `project.path` (the project root). The
    dispatcher converts them to strings before writing them into the manifest.
    """

    status: StageStatus
    outputs: tuple[Path, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: str | None = None
    error: ErrorRecord | None = None


class Stage(Protocol):
    """Structural type for a pipeline stage.

    Concrete stages live under `shipcast.stages.s<NN>_<name>` and inherit from
    `BaseStage`. The Protocol exists so the dispatcher can accept any object
    that satisfies this shape (including future test doubles).
    """

    id: str
    requires: tuple[str, ...]
    review_checklist_items: tuple[str, ...]
    output_schema: type[BaseModel]

    def check_inputs(self, project: Project) -> None: ...
    def run(self, project: Project) -> StageResult: ...
    def validate_outputs(self, project: Project, result: StageResult) -> None: ...
    def pre_run_hook(self, project: Project) -> None: ...
