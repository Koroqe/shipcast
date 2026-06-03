"""Slice 2 — cost ledger + dispatcher pre-call cost-cap gate.

Owned TCs:
- TC-17.1: accumulated $2.97 + $0.04 Imagen → `CostCapExceeded`, client mock NEVER called.
- TC-17.5: accumulated exactly $3.00 + $0.04 → `CostCapExceeded` (projected $3.04 > $3.00).
- TC-17.2: accumulated $0.50 + $0.04 proceeds; `metrics.cost_usd` updated after the call.
- TC-17.6: reset-then-rerun starts that stage's cost from $0 with no double-count.

Security focus (Slice 2 is security pre-review flagged):
- The cap check is a TRUE pre-condition: `projected` is computed and compared
  BEFORE `run()` is reached, so the (paid) client is never invoked over cap.
- `metrics.cost_usd` accumulation is monotonic and cannot underflow.
- No double-count on rerun/reset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import shipcast.cli as cli
import shipcast.stages as _stages
from shipcast.cost import (
    ELEVENLABS_PER_MINUTE_USD,
    GEMINI_MULTIMODAL_CALL_USD,
    IMAGEN_IMAGE_USD,
    VEO_FAST_CLIP_USD,
    CostLedger,
    accumulated_cost_usd,
)
from shipcast.errors import CostCapExceeded
from shipcast.manifest import Manifest, StageStatus
from shipcast.project import Project
from shipcast.stage import StageResult
from shipcast.stages import BaseStage

runner = CliRunner()


# --------------------------------------------------------------------------- #
# Unit-cost constants
# --------------------------------------------------------------------------- #


def test_unit_cost_constants_match_pricing() -> None:
    """The four per-tool unit costs are the honest published prices."""
    assert VEO_FAST_CLIP_USD == 3.20
    assert IMAGEN_IMAGE_USD == 0.04
    assert GEMINI_MULTIMODAL_CALL_USD == 0.01
    assert ELEVENLABS_PER_MINUTE_USD == 0.30


def test_unit_costs_are_non_negative() -> None:
    """No unit cost may be negative — accumulation must be monotonic."""
    for cost in (
        VEO_FAST_CLIP_USD,
        IMAGEN_IMAGE_USD,
        GEMINI_MULTIMODAL_CALL_USD,
        ELEVENLABS_PER_MINUTE_USD,
    ):
        assert cost >= 0.0


# --------------------------------------------------------------------------- #
# accumulated_cost_usd — sums `stages[*].metrics.cost_usd` defensively
# --------------------------------------------------------------------------- #


def _with_costs(manifest: Manifest, **cost_by_stage: float | None) -> Manifest:
    """Return a copy of `manifest` with the given stage `cost_usd` metrics set."""
    stages = dict(manifest.stages)
    for stage_id, cost in cost_by_stage.items():
        stages[stage_id] = stages[stage_id].model_copy(
            update={"metrics": {"cost_usd": cost}}
        )
    return manifest.model_copy(update={"stages": stages})


def test_accumulated_sums_across_stages(make_project: Any) -> None:
    """Accumulated cost is the sum of every stage's `metrics.cost_usd`."""
    manifest = _with_costs(make_project().manifest, **{"01_pick": 0.01, "02_enrich": 0.04})
    assert accumulated_cost_usd(manifest) == pytest.approx(0.05)


def test_accumulated_ignores_missing_metric(make_project: Any) -> None:
    """Stages without a `cost_usd` metric contribute 0, never an error."""
    manifest = _with_costs(make_project().manifest, **{"02_enrich": 0.04})
    assert accumulated_cost_usd(manifest) == pytest.approx(0.04)


def test_accumulated_treats_none_as_zero(make_project: Any) -> None:
    """A `cost_usd` of None contributes 0 (defensive against partial writes)."""
    manifest = _with_costs(make_project().manifest, **{"01_pick": None})
    assert accumulated_cost_usd(manifest) == pytest.approx(0.0)


def test_accumulated_rejects_negative_cost(make_project: Any) -> None:
    """A negative `cost_usd` is treated as a corrupt manifest — must raise.

    Accumulation must be monotonic and cannot underflow; a negative metric
    can only mean tampering or a bug, never a legitimate refund.
    """
    manifest = _with_costs(make_project().manifest, **{"01_pick": -1.0})
    with pytest.raises(ValueError, match="negative"):
        accumulated_cost_usd(manifest)


# --------------------------------------------------------------------------- #
# CostLedger.projected / would_exceed
# --------------------------------------------------------------------------- #


def test_ledger_projected_adds_next_unit_cost(make_project: Any) -> None:
    """`projected(next)` == accumulated + next, never mutating the manifest."""
    manifest = _with_costs(make_project().manifest, **{"01_pick": 0.50})
    ledger = CostLedger(manifest)
    assert ledger.accumulated() == pytest.approx(0.50)
    assert ledger.projected(IMAGEN_IMAGE_USD) == pytest.approx(0.54)
    assert ledger.accumulated() == pytest.approx(0.50)  # no mutation


def test_ledger_would_exceed_strict_greater_than(make_project: Any) -> None:
    """`would_exceed` uses strict `>`: exactly-at-cap does NOT exceed."""
    manifest = _with_costs(make_project().manifest, **{"01_pick": 3.00})
    ledger = CostLedger(manifest)
    assert ledger.would_exceed(0.0, cap=3.00) is False  # projected == cap
    assert ledger.would_exceed(IMAGEN_IMAGE_USD, cap=3.00) is True  # over cap


def test_ledger_rejects_negative_unit_cost(make_project: Any) -> None:
    """A negative `next_unit_cost` is rejected — costs only ever add."""
    ledger = CostLedger(make_project().manifest)
    with pytest.raises(ValueError, match="negative"):
        ledger.projected(-0.01)


# --------------------------------------------------------------------------- #
# Dispatcher pre-call cost-cap gate — injected fake paid stage
# --------------------------------------------------------------------------- #


class _PaidOutput(BaseModel):
    ok: bool


#: Sentinel proving the (paid) client body in `run()` was reached.
_PAID_CALLS: list[str] = []


class _FakePaidStage(BaseStage):
    """A first-stage fake that declares a unit cost and 'calls a paid client'.

    The gate must fire BEFORE `run()` executes when over cap, so `_PAID_CALLS`
    stays empty — the mock-never-called assertion (TC-17.1/TC-17.5).
    """

    id: ClassVar[str] = "01_pick"
    requires: ClassVar[tuple[str, ...]] = ()
    output_schema: ClassVar[type[BaseModel]] = _PaidOutput
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Confirm the paid call produced the expected artifact.",
    )
    unit_cost_usd: ClassVar[float] = IMAGEN_IMAGE_USD

    def next_call_cost_usd(self, project: Project) -> float:
        return self.unit_cost_usd

    def run(self, project: Project) -> StageResult:
        _PAID_CALLS.append(self.id)  # the paid call
        out = project.stage_dir(self.id) / "out.json"
        out.write_text('{\n  "ok": true\n}\n', encoding="utf-8")
        return StageResult(
            status=StageStatus.DONE,
            outputs=(Path(self.id) / "out.json",),
            metrics={"cost_usd": self.unit_cost_usd},
        )


@pytest.fixture(autouse=True)
def _clear_calls() -> Any:
    _PAID_CALLS.clear()
    yield
    _PAID_CALLS.clear()


def _seed_accumulated_cost(project: Project, cost: float) -> None:
    """Park `cost` on an unrelated DONE stage so the gated stage starts at 0."""
    record = project.manifest.stages["11_package"]
    manifest = project.manifest.model_copy(
        update={
            "stages": {
                **project.manifest.stages,
                "11_package": record.model_copy(
                    update={"status": StageStatus.DONE, "metrics": {"cost_usd": cost}}
                ),
            }
        }
    )
    project.with_manifest(manifest).save_manifest()


def test_tc_17_1_cost_cap_blocks_before_paid_call(
    make_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-17.1: $2.97 + $0.04 Imagen → CostCapExceeded; paid call never made."""
    project = make_project()
    _seed_accumulated_cost(project, 2.97)
    monkeypatch.setattr(_stages, "ALL_STAGES", (_FakePaidStage,))

    result = runner.invoke(
        cli.app, ["--projects-root", str(project.root), "pick", project.slug]
    )

    assert result.exit_code != 0, result.output
    assert _PAID_CALLS == [], "paid client was invoked despite being over cap"
    reloaded = Project.load(project.root, project.slug)
    record = reloaded.manifest.stages["01_pick"]
    assert record.status == StageStatus.FAILED
    assert record.error is not None
    assert record.error.type == CostCapExceeded.__name__


def test_tc_17_5_cost_exactly_at_cap_blocks(
    make_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-17.5: accumulated == $3.00, next $0.04 → projected $3.04 > $3.00 → block."""
    project = make_project()
    _seed_accumulated_cost(project, 3.00)
    monkeypatch.setattr(_stages, "ALL_STAGES", (_FakePaidStage,))

    result = runner.invoke(
        cli.app, ["--projects-root", str(project.root), "pick", project.slug]
    )

    assert result.exit_code != 0, result.output
    assert _PAID_CALLS == []
    reloaded = Project.load(project.root, project.slug)
    assert reloaded.manifest.stages["01_pick"].status == StageStatus.FAILED


def test_tc_17_2_within_cap_proceeds_and_records_cost(
    make_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-17.2: $0.50 + $0.04 within $3.00 → run proceeds; metrics.cost_usd recorded."""
    project = make_project()
    _seed_accumulated_cost(project, 0.50)
    monkeypatch.setattr(_stages, "ALL_STAGES", (_FakePaidStage,))

    result = runner.invoke(
        cli.app, ["--projects-root", str(project.root), "pick", project.slug]
    )

    assert result.exit_code == 0, result.output
    assert _PAID_CALLS == ["01_pick"], "paid call should have been made within cap"
    reloaded = Project.load(project.root, project.slug)
    record = reloaded.manifest.stages["01_pick"]
    assert record.status == StageStatus.DONE
    assert record.metrics["cost_usd"] == pytest.approx(IMAGEN_IMAGE_USD)
    assert accumulated_cost_usd(reloaded.manifest) == pytest.approx(0.54)


def test_tc_17_6_rerun_clears_stage_cost_no_double_count(
    make_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-17.6: rerun clears that stage's cost_usd, so re-accumulation never doubles."""
    project = make_project()
    _seed_accumulated_cost(project, 0.50)
    monkeypatch.setattr(_stages, "ALL_STAGES", (_FakePaidStage,))

    first = runner.invoke(
        cli.app, ["--projects-root", str(project.root), "pick", project.slug]
    )
    assert first.exit_code == 0, first.output
    after_first = Project.load(project.root, project.slug)
    assert accumulated_cost_usd(after_first.manifest) == pytest.approx(0.54)

    second = runner.invoke(
        cli.app,
        ["--projects-root", str(project.root), "pick", project.slug, "--rerun", "--yes"],
    )
    assert second.exit_code == 0, second.output
    after_second = Project.load(project.root, project.slug)
    # Still 0.54, NOT 0.58: the stage's prior 0.04 was cleared by reset.
    assert accumulated_cost_usd(after_second.manifest) == pytest.approx(0.54)
    assert after_second.manifest.stages["01_pick"].metrics["cost_usd"] == pytest.approx(
        IMAGEN_IMAGE_USD
    )
