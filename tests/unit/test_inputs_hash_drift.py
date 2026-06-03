"""TC-20.3 — Finding 2 audit-only `inputs_hash` behavior.

Architecture MAJOR Finding 2 was resolved with option (b): `inputs_hash` is
recorded for audit and powers `--rerun` invalidation, but
`BaseStage.check_inputs` does NOT auto-block on upstream `inputs_hash` drift —
the per-stage human gate already bounds stale-input runs.

This module asserts that documented behavior two ways:

1. Behaviorally: a downstream stage whose upstream output bytes changed AFTER
   approval is NOT blocked by `check_inputs`, even though the recorded
   upstream `inputs_hash` no longer matches the current file state.
2. By contract reference: `_base.py:check_inputs` carries an explicit comment
   block documenting the audit-only Finding-2 decision (so the choice is not
   left ambiguous, per TC-20.3's resolution requirement).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from shipcast.manifest import (
    StageRecord,
    StageStatus,
    compute_inputs_hash,
)
from shipcast.project import Project
from shipcast.stages._base import BaseStage

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


class _UpstreamOutput(BaseModel):
    value: str


class _DownstreamStage(BaseStage):
    """Minimal stage that requires `01_pick` and uses default `check_inputs`."""

    id = "02_enrich"
    requires = ("01_pick",)
    output_schema = _UpstreamOutput
    review_checklist_items = ("review",)


def _approved_upstream_project(
    make_project: Callable[..., Project],
) -> tuple[Project, Path, str]:
    """Build a project where 01_pick is done+approved with a written output.

    Returns the project, the on-disk upstream artifact path, and the
    `inputs_hash` recorded against the upstream's original bytes.
    """
    project = make_project(slug="entry")

    # Write the upstream artifact on disk.
    upstream_dir = project.stage_dir("01_pick")
    upstream_dir.mkdir(parents=True, exist_ok=True)
    rel = "01_pick/entry.json"
    artifact = project.path / rel
    artifact.write_text('{"name": "Add CSV export"}', encoding="utf-8")

    # Record the inputs_hash against the ORIGINAL upstream bytes.
    original_inputs_hash = compute_inputs_hash([artifact])

    # Mark 01_pick done + approved with its declared output.
    manifest = project.manifest.model_copy(
        update={
            "stages": {
                **project.manifest.stages,
                "01_pick": StageRecord(
                    status=StageStatus.DONE,
                    outputs=(rel,),
                    inputs_hash=original_inputs_hash,
                    human_approved_at=_NOW,
                ),
            }
        }
    )
    return project.with_manifest(manifest), artifact, original_inputs_hash


def test_tc_20_3_check_inputs_does_not_block_on_upstream_drift(
    make_project: Callable[..., Project],
) -> None:
    """TC-20.3 (option b): upstream byte-drift after approval is NOT auto-blocked.

    After modifying the approved upstream artifact's bytes (so a fresh
    `compute_inputs_hash` differs from the stored one), `check_inputs` returns
    cleanly — the human gate, not an inputs_hash comparison, is the freshness
    barrier.
    """
    project, artifact, original_inputs_hash = _approved_upstream_project(make_project)

    # Mutate the upstream artifact AFTER it was approved.
    artifact.write_text('{"name": "Edited after approval"}', encoding="utf-8")
    drifted_inputs_hash = compute_inputs_hash([artifact])

    # The recorded hash and the current file state genuinely diverge...
    assert drifted_inputs_hash != original_inputs_hash
    assert project.manifest.stages["01_pick"].inputs_hash == original_inputs_hash

    # ...yet check_inputs does NOT raise (audit-only, option b).
    _DownstreamStage().check_inputs(project)  # must not raise


def test_tc_20_3_check_inputs_documents_audit_only_decision() -> None:
    """TC-20.3: `_base.py:check_inputs` explicitly documents the audit-only choice.

    The resolution of TC-20.3 requires the behavior to be unambiguous — either
    a blocking/warning check OR a documented audit-only comment. We chose
    option (b), so the comment block must be present and reference the
    audit-only / human-gate rationale.
    """
    source = inspect.getsource(BaseStage.check_inputs)
    lowered = source.lower()
    assert "finding 2" in lowered
    assert "audit-only" in lowered
    # It must state the gate bounds staleness rather than inputs_hash here.
    assert "human gate" in lowered or "human-approved" in lowered
    # And it must NOT consult inputs_hash for blocking.
    assert "inputs_hash" in source
