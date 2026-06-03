"""Concrete pipeline stages.

`BaseStage` ships shared check_inputs / validate_outputs defaults; each stage
module under this package defines one concrete subclass declaring its `id`,
`requires`, `output_schema`, and `run` body.

Slice 1 ships the scaffold only: ``ALL_STAGES`` is empty and concrete stage
classes land in later slices (``s01_pick`` in Slice 6, ``s02_enrich`` in
Slice 7, …). The dispatcher therefore lists every verb in ``shipcast --help``
but no stage runs until its slice lands. As each stage is added, append its
class to ``ALL_STAGES`` in pipeline order.
"""

from __future__ import annotations

from shipcast.stages._base import BaseStage
from shipcast.stages.s01_pick import PickStage
from shipcast.stages.s02_enrich import EnrichStage
from shipcast.stages.s03_brand import BrandStage
from shipcast.stages.s04_plan import PlanStage
from shipcast.stages.s05_script import ScriptStage

#: All concrete stages in pipeline order. Useful for the dispatcher and
#: integration tests. Grows one entry per stage slice (Slice 6 onward).
ALL_STAGES: tuple[type[BaseStage], ...] = (
    PickStage,
    EnrichStage,
    BrandStage,
    PlanStage,
    ScriptStage,
)


def build_downstream_map() -> dict[str, tuple[str, ...]]:
    """Return the reverse-dependency map: upstream stage_id → tuple of immediate downstream ids.

    Used by `Manifest.reset` for the transitive cascade and by the CLI's
    cascade-confirmation guard.
    """
    downstream: dict[str, list[str]] = {cls.id: [] for cls in ALL_STAGES}
    for cls in ALL_STAGES:
        for upstream_id in cls.requires:
            if upstream_id in downstream:
                downstream[upstream_id].append(cls.id)
    return {stage_id: tuple(deps) for stage_id, deps in downstream.items()}


__all__ = [
    "ALL_STAGES",
    "BaseStage",
    "BrandStage",
    "EnrichStage",
    "PickStage",
    "PlanStage",
    "ScriptStage",
    "build_downstream_map",
]
