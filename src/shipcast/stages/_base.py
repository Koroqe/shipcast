"""BaseStage — shared default behavior for all pipeline stages.

Subclasses declare `id`, `requires`, `output_schema`, `review_checklist_items`,
and implement `run()`. They MAY override `check_inputs`, `validate_outputs`,
`additional_input_paths`, or `pre_run_hook` for stage-specific behavior, but
the defaults here are sufficient for most stages.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ValidationError

from shipcast.errors import StageInputMissing, StageNotApproved, StageOutputInvalid
from shipcast.manifest import StageStatus, compute_inputs_hash
from shipcast.stage import StageResult

if TYPE_CHECKING:
    from shipcast.project import Project


class BaseStage:
    """Shared scaffolding for concrete stages.

    Class attributes that concrete stages must override:

    * `id` — the manifest key, e.g. `"01_pick"`.
    * `requires` — tuple of immediate-upstream stage ids.
    * `output_schema` — the Pydantic model that validates this stage's output.
    * `review_checklist_items` — non-empty tuple of human-review prompts.

    Methods concrete stages typically override:

    * `run(project)` — the side-effecting body.

    Methods concrete stages MAY override:

    * `check_inputs(project)` — additional input-existence checks beyond the
      default upstream-stage validation.
    * `validate_outputs(project, result)` — additional output validation
      beyond the default single-schema check.
    * `additional_input_paths(project)` — extra files (outside the upstream
      stage's outputs) whose changes should invalidate `inputs_hash`.
    * `pre_run_hook(project)` — test-only seam invoked before `run()`.
    """

    id: ClassVar[str]
    requires: ClassVar[tuple[str, ...]] = ()
    output_schema: ClassVar[type[BaseModel]]
    review_checklist_items: ClassVar[tuple[str, ...]] = ()
    #: Set True on stages whose `run()` shells out to ffmpeg. The dispatcher
    #: calls `FfmpegClient.check_available()` BEFORE acquiring the project
    #: lock when this is True, so a missing binary fails fast without
    #: side effects.
    requires_ffmpeg: ClassVar[bool] = False

    # -------------------------------------------------------------- defaults

    def check_inputs(self, project: Project) -> None:
        """Verify upstream stages are done + approved and their outputs exist.

        Raises:
            StageInputMissing: upstream stage missing/not-done, OR upstream
                artifact file absent on disk.
            StageNotApproved: upstream stage is done but lacks
                `human_approved_at`.
        """
        for upstream_id in self.requires:
            record = project.manifest.stages.get(upstream_id)
            if record is None:
                raise StageInputMissing(
                    f"stage {self.id!r} requires {upstream_id!r}, which is not in the manifest"
                )
            if record.status != StageStatus.DONE:
                raise StageInputMissing(
                    f"stage {self.id!r} requires {upstream_id!r} to be done "
                    f"(current status: {record.status.value})"
                )
            if record.human_approved_at is None:
                raise StageNotApproved(
                    f"stage {self.id!r} requires {upstream_id!r} to be human-approved"
                )
            for rel_path in record.outputs:
                full = project.path / rel_path
                if not full.is_file():
                    raise StageInputMissing(
                        f"stage {self.id!r} requires {upstream_id!r}'s output {rel_path!r} "
                        f"to exist on disk at {full}"
                    )

    def _validate_output_paths(self, project: Project, result: StageResult) -> None:
        """Reject outputs that are absolute, escape `stage_dir`, or do not exist.

        Defense-in-depth shared by `validate_outputs` AND by stages that
        override `validate_outputs` for binary artifacts (e.g., stage 03's
        MP3 magic-byte check still needs the path-traversal guards). Stage 03
        Slice 1 / architect REC-7.

        Raises:
            StageOutputInvalid: on absolute path, `..`-escape, or missing file.
        """
        if not result.outputs:
            return
        stage_dir = project.stage_dir(self.id).resolve()
        for rel_path in result.outputs:
            if rel_path.is_absolute():
                raise StageOutputInvalid(
                    f"stage {self.id!r} returned absolute output path {rel_path}; "
                    f"must be project-relative"
                )
            resolved = (project.path / rel_path).resolve()
            if not resolved.is_relative_to(stage_dir):
                raise StageOutputInvalid(
                    f"stage {self.id!r} declared output {rel_path!r} which resolves "
                    f"outside its stage_dir {stage_dir}"
                )
            if not resolved.is_file():
                raise StageOutputInvalid(
                    f"stage {self.id!r} declared output {rel_path!r} but file is missing at {resolved}"
                )

    def validate_outputs(self, project: Project, result: StageResult) -> None:
        """Default: confirm every declared output exists; if exactly one, validate it against output_schema.

        Stages with multiple heterogeneous outputs should override.

        Also rejects:
        - Absolute paths in `result.outputs` (must be project-relative for portability).
        - Paths that escape the stage's `stage_dir` via `..` components
          (defense-in-depth against malicious/buggy stages).
        """
        if not result.outputs:
            return
        self._validate_output_paths(project, result)
        if len(result.outputs) == 1:
            full = (project.path / result.outputs[0]).resolve()
            try:
                data = json.loads(full.read_text(encoding="utf-8"))
                self.output_schema.model_validate(data)
            except json.JSONDecodeError as exc:
                raise StageOutputInvalid(
                    f"stage {self.id!r} output {full} is not valid JSON: {exc}"
                ) from exc
            except ValidationError as exc:
                raise StageOutputInvalid(
                    f"stage {self.id!r} output {full} failed schema validation: {exc}"
                ) from exc

    def additional_input_paths(self, project: Project) -> Iterable[Path]:
        """Hook for stages that consume operator-placed files outside the upstream stage's outputs.

        Default: empty. Stage 9 (image generation) will override this to include
        `09_images/references/**`.
        """
        return ()

    def next_call_cost_usd(self, project: Project) -> float:
        """USD the stage's next paid API call would incur — the cost-cap charge.

        The dispatcher reads this BEFORE invoking `run()` and refuses to proceed
        (raising `CostCapExceeded`) when `accumulated + this > cap`. Returning
        the cost here — rather than charging inside `run()` — makes the cap a
        TRUE pre-condition: a stage that is over budget never constructs or
        calls its (paid) client.

        Default `0.0`: stages that call no paid API (pure/deterministic stages)
        are never gated. Paid stages override this to return the unit cost of
        the most expensive single call their `run()` would make (e.g. one
        Imagen image, one Veo clip), sourced from `shipcast.cost` constants.
        """
        return 0.0

    def pre_run_hook(self, project: Project) -> None:
        """Test-only seam invoked by the dispatcher before `run()`.

        Production stages MUST NOT override this. The two-process race test in
        Slice 6 overrides it to inject a sleep without resorting to env var
        branching inside production code.
        """

    def run(self, project: Project) -> StageResult:
        """Execute the stage. Concrete subclasses MUST override.

        The dispatcher calls this AFTER `check_inputs` and `pre_run_hook`,
        then validates the returned `StageResult` via `validate_outputs`.
        """
        raise NotImplementedError(
            f"BaseStage subclass {type(self).__name__} must implement run()"
        )

    # -------------------------------------------------------------- helpers

    def upstream_artifact_paths(self, project: Project) -> list[Path]:
        """Return absolute paths of every IMMEDIATE upstream stage's outputs.

        Used by the dispatcher to compute `inputs_hash` via
        `compute_inputs_hash`. Does NOT include transitive ancestors —
        reproducibility checks are local and predictable.
        """
        paths: list[Path] = []
        for upstream_id in self.requires:
            record = project.manifest.stages.get(upstream_id)
            if record is None:
                continue
            for rel_path in record.outputs:
                paths.append(project.path / rel_path)
        return paths

    def compute_stage_inputs_hash(self, project: Project) -> str:
        """Compute this stage's `inputs_hash` from immediate-upstream outputs + additional inputs.

        Convenience wrapper the dispatcher calls. Concrete stages should not
        need to override this; if they need extra inputs in the hash, they
        override `additional_input_paths` instead.
        """
        paths = self.upstream_artifact_paths(project)
        paths.extend(self.additional_input_paths(project))
        return compute_inputs_hash(paths)
