"""Cost-ledger full-pipeline integration test (Slice 21).

Drives all 11 pipeline stages end-to-end through the REAL CLI dispatcher
(`shipcast.cli`) with EVERY external client mocked away, and asserts the cost
discipline holds across the whole pipeline:

Owned TCs:
- TC-17.3: standard-mode full run → total accumulated `metrics.cost_usd` ≤ $3.00.
- TC-17.4: premium-mode full run (Veo 3 Fast hero $3.20) → total ≤ $8.00.
- TC-23.1 (cost slice): the dispatcher's `_enforce_cost_cap` gate aborts a paid
  stage with `CostCapExceeded` when the projected cost would exceed the cap, and
  the stage's paid client is NEVER constructed/called.

Design — why fake cost-bearing stages, not the real heavy stages
----------------------------------------------------------------
This test exercises the *cost ledger + dispatcher gate*, not the per-stage
rendering. Driving the real Imagen/Veo/ElevenLabs/WhisperX/Playwright/ffmpeg
stack through 11 stages would be slow and brittle and is already covered by each
stage's own integration test. Here we register, in the live `ALL_STAGES`
registry, a faithful set of fake stages that:

* mirror the real 11 stage ids and the real upstream `requires` DAG,
* declare the SAME `next_call_cost_usd` (so the real `_enforce_cost_cap` gate
  charges the real per-tool unit costs), and
* record the SAME `metrics.cost_usd` the real stages record on DONE.

The unit costs come straight from `shipcast.cost`, so the asserted totals track
the production constants. The dispatcher, manifest accumulation, human-gate
approval, and the `_enforce_cost_cap` pre-call gate are all the REAL code paths.

No real external API / network / subprocess is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import shipcast.cli as cli
import shipcast.stages as _stages
from shipcast.config import Settings
from shipcast.cost import (
    ELEVENLABS_PER_MINUTE_USD,
    GEMINI_MULTIMODAL_CALL_USD,
    IMAGEN_IMAGE_USD,
    VEO_FAST_CLIP_USD,
    accumulated_cost_usd,
)
from shipcast.manifest import Manifest, StageStatus
from shipcast.project import Project
from shipcast.stage import StageResult
from shipcast.stages import BaseStage

runner = CliRunner()

# --------------------------------------------------------------------------- #
# Faithful per-stage cost model (mirrors the real stages, from shipcast.cost)
# --------------------------------------------------------------------------- #

#: ElevenLabs estimate for a ~300-char narration: chars to words to minutes x rate.
#: The real `VoiceStage.next_call_cost_usd` estimates from the storyboard text;
#: we pin a representative ~0.20-minute clip so the fake's gate-cost == metric.
_VOICE_MINUTES: float = 0.2
_VOICE_COST: float = round(ELEVENLABS_PER_MINUTE_USD * _VOICE_MINUTES, 4)

#: Standard graphics: 4 aspect cards + 1 OG card = 5 Imagen calls (no stat card
#: in this scenario; the carousel + code screenshot are pure-PIL, no cost).
_GRAPHICS_IMAGEN_CARDS: int = 5
_GRAPHICS_COST: float = round(IMAGEN_IMAGE_USD * _GRAPHICS_IMAGEN_CARDS, 4)


class _Ok(BaseModel):
    ok: bool


class _CostStage(BaseStage):
    """A fake stage that records a fixed `cost_usd` and writes one JSON output.

    Subclasses set `id`, `requires`, the recorded `_cost`, and the gate-cost via
    `next_call_cost_usd`. The written output lets downstream `check_inputs`
    (which verifies declared outputs exist on disk) pass.
    """

    output_schema: ClassVar[type[BaseModel]] = _Ok
    review_checklist_items: ClassVar[tuple[str, ...]] = ("Review the artifact.",)

    #: USD recorded into `metrics.cost_usd` on DONE (what the real stage incurs).
    _cost: ClassVar[float] = 0.0
    #: USD the dispatcher charges against the cap BEFORE run (real gate cost).
    _gate_cost: ClassVar[float] = 0.0

    def next_call_cost_usd(self, project: Project) -> float:
        return self._gate_cost

    def run(self, project: Project) -> StageResult:
        out_dir = project.stage_dir(self.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "out.json"
        out_file.write_text('{\n  "ok": true\n}\n', encoding="utf-8")
        rel = out_file.relative_to(project.path)
        return StageResult(
            status=StageStatus.DONE,
            outputs=(rel,),
            metrics={"cost_usd": self._cost},
        )


# Free (no paid call) stages — cost 0, gate never fires.
class _PickStage(_CostStage):
    id: ClassVar[str] = "01_pick"
    requires: ClassVar[tuple[str, ...]] = ()


class _PlanStage(_CostStage):
    id: ClassVar[str] = "04_plan"
    requires: ClassVar[tuple[str, ...]] = ("03_brand",)


class _ScriptStage(_CostStage):
    id: ClassVar[str] = "05_script"
    requires: ClassVar[tuple[str, ...]] = ("04_plan",)


class _VideoStage(_CostStage):
    id: ClassVar[str] = "08_video"
    requires: ClassVar[tuple[str, ...]] = ("06_video_assets", "07_voice")


class _CopyStage(_CostStage):
    id: ClassVar[str] = "10_copy"
    requires: ClassVar[tuple[str, ...]] = ("04_plan",)


class _PackageStage(_CostStage):
    id: ClassVar[str] = "11_package"
    requires: ClassVar[tuple[str, ...]] = ("08_video", "09_graphics", "10_copy")


# Paid stages — real unit costs from shipcast.cost.
class _EnrichStage(_CostStage):
    id: ClassVar[str] = "02_enrich"
    requires: ClassVar[tuple[str, ...]] = ("01_pick",)
    _cost: ClassVar[float] = GEMINI_MULTIMODAL_CALL_USD
    _gate_cost: ClassVar[float] = GEMINI_MULTIMODAL_CALL_USD


class _BrandStage(_CostStage):
    id: ClassVar[str] = "03_brand"
    requires: ClassVar[tuple[str, ...]] = ("02_enrich",)
    _cost: ClassVar[float] = IMAGEN_IMAGE_USD
    _gate_cost: ClassVar[float] = IMAGEN_IMAGE_USD


class _VoiceStage(_CostStage):
    id: ClassVar[str] = "07_voice"
    requires: ClassVar[tuple[str, ...]] = ("05_script",)
    _cost: ClassVar[float] = _VOICE_COST
    _gate_cost: ClassVar[float] = _VOICE_COST


class _GraphicsStage(_CostStage):
    id: ClassVar[str] = "09_graphics"
    requires: ClassVar[tuple[str, ...]] = ("04_plan", "03_brand")
    _cost: ClassVar[float] = _GRAPHICS_COST
    #: gate charges the single most-expensive Imagen call (one card).
    _gate_cost: ClassVar[float] = IMAGEN_IMAGE_USD


class _VideoAssetsStage(_CostStage):
    """Mode-dependent: standard → 4 Imagen Ken-Burns; premium → Veo + 3 Imagen.

    Reads the loaded `project.settings.video_mode` exactly as the real stage's
    `_resolve_mode` does, so the same dispatcher path is exercised per mode.
    """

    id: ClassVar[str] = "06_video_assets"
    requires: ClassVar[tuple[str, ...]] = ("05_script",)

    @staticmethod
    def _premium(project: Project) -> bool:
        return project.settings.video_mode == "premium"

    def next_call_cost_usd(self, project: Project) -> float:
        # Most-expensive single call: Veo clip (premium) else one Imagen still.
        return VEO_FAST_CLIP_USD if self._premium(project) else IMAGEN_IMAGE_USD

    def run(self, project: Project) -> StageResult:
        if self._premium(project):
            cost = round(VEO_FAST_CLIP_USD + 3 * IMAGEN_IMAGE_USD, 4)
        else:
            cost = round(4 * IMAGEN_IMAGE_USD, 4)
        out_dir = project.stage_dir(self.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "out.json"
        out_file.write_text('{\n  "ok": true\n}\n', encoding="utf-8")
        rel = out_file.relative_to(project.path)
        return StageResult(
            status=StageStatus.DONE,
            outputs=(rel,),
            metrics={"cost_usd": cost},
        )


#: The fake pipeline registry, in topological dispatch order.
_FAKE_STAGES: tuple[type[BaseStage], ...] = (
    _PickStage,
    _EnrichStage,
    _BrandStage,
    _PlanStage,
    _ScriptStage,
    _VideoAssetsStage,
    _VoiceStage,
    _VideoStage,
    _GraphicsStage,
    _CopyStage,
    _PackageStage,
)

#: Dispatch order: every stage runs only after its upstreams are done+approved.
_DISPATCH_ORDER: tuple[tuple[str, str], ...] = (
    ("pick", "01_pick"),
    ("enrich", "02_enrich"),
    ("brand", "03_brand"),
    ("plan", "04_plan"),
    ("script", "05_script"),
    ("graphics", "09_graphics"),
    ("copy", "10_copy"),
    ("video_assets", "06_video_assets"),
    ("voice", "07_voice"),
    ("video", "08_video"),
    ("package", "11_package"),
)

#: Documented per-mode estimates (architecture cost discipline) — the asserted
#: totals must land near these and strictly under the per-mode cap.
_STANDARD_CAP: float = 3.0
_PREMIUM_CAP: float = 8.0


# --------------------------------------------------------------------------- #
# Fixtures + helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


def _install_fake_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the live stage registry with the faithful fake cost stages."""
    monkeypatch.setattr(_stages, "ALL_STAGES", _FAKE_STAGES)


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Make every `Project.load` report the requested video_mode (and its cap)."""
    real_from_files = Settings.from_files.__func__  # type: ignore[attr-defined]

    def _from_files(cls: Any, *a: Any, **k: Any) -> Settings:
        settings = real_from_files(cls, *a, **k)
        return settings.model_copy(update={"video_mode": mode})

    monkeypatch.setattr(Settings, "from_files", classmethod(_from_files))


def _create_project(projects_root: Path) -> str:
    """Materialize a bare project (all 11 stages pending) and return its slug."""
    from shipcast.paths import default_template_path

    slug = "cost-cap-entry"
    Project.create(
        projects_root,
        slug,
        {},
        settings=Settings(),
        template_path=default_template_path(),
    )
    return slug


def _root(projects_root: Path) -> list[str]:
    return ["--projects-root", str(projects_root)]


def _run_full_pipeline(projects_root: Path, slug: str) -> Manifest:
    """Dispatch + approve all 11 stages in order; return the final manifest."""
    for verb, stage_id in _DISPATCH_ORDER:
        result = runner.invoke(cli.app, [*_root(projects_root), verb, slug])
        assert result.exit_code == 0, f"{verb} failed: {result.output}"
        result = runner.invoke(cli.app, [*_root(projects_root), "approve", slug, stage_id])
        assert result.exit_code == 0, f"approve {stage_id} failed: {result.output}"
    return Manifest.load(projects_root / slug / "manifest.json")


# --------------------------------------------------------------------------- #
# TC-17.3 — standard-mode full pipeline total ≤ $3.00
# --------------------------------------------------------------------------- #


def test_tc_17_3_standard_pipeline_under_cap(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-17.3: standard-mode 11-stage run accumulates ≤ $3.00."""
    _install_fake_registry(monkeypatch)
    _set_mode(monkeypatch, "standard")
    slug = _create_project(projects_root)

    manifest = _run_full_pipeline(projects_root, slug)

    # Every stage reached done+approved.
    assert all(r.status == StageStatus.DONE for r in manifest.stages.values())
    assert all(r.human_approved_at is not None for r in manifest.stages.values())

    total = accumulated_cost_usd(manifest)
    # enrich 0.01 + brand 0.04 + video_assets 0.16 + voice 0.06 + graphics 0.20.
    expected = (
        GEMINI_MULTIMODAL_CALL_USD
        + IMAGEN_IMAGE_USD
        + round(4 * IMAGEN_IMAGE_USD, 4)
        + _VOICE_COST
        + _GRAPHICS_COST
    )
    assert total == pytest.approx(expected, abs=1e-6)
    assert total <= _STANDARD_CAP
    # Sanity: the standard total tracks the documented ~$0.83 estimate band.
    assert 0.4 <= total <= 1.0, total


# --------------------------------------------------------------------------- #
# TC-17.4 — premium-mode full pipeline total ≤ $8.00
# --------------------------------------------------------------------------- #


def test_tc_17_4_premium_pipeline_under_cap(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-17.4: premium-mode 11-stage run (Veo $3.20 hero) accumulates ≤ $8.00."""
    _install_fake_registry(monkeypatch)
    _set_mode(monkeypatch, "premium")
    slug = _create_project(projects_root)

    manifest = _run_full_pipeline(projects_root, slug)

    assert all(r.status == StageStatus.DONE for r in manifest.stages.values())

    total = accumulated_cost_usd(manifest)
    # video_assets in premium: Veo 3.20 + 3 Imagen 0.12 = 3.32.
    expected = (
        GEMINI_MULTIMODAL_CALL_USD
        + IMAGEN_IMAGE_USD
        + round(VEO_FAST_CLIP_USD + 3 * IMAGEN_IMAGE_USD, 4)
        + _VOICE_COST
        + _GRAPHICS_COST
    )
    assert total == pytest.approx(expected, abs=1e-6)
    assert total <= _PREMIUM_CAP
    # Sanity: premium total tracks the documented ~$4.03 estimate band.
    assert 3.5 <= total <= 4.5, total
    # And it exceeds the standard cap — i.e. premium genuinely needs its raise.
    assert total > _STANDARD_CAP


# --------------------------------------------------------------------------- #
# TC-23.1 (cost) — cap gate aborts a paid stage before its client is called
# --------------------------------------------------------------------------- #


class _ExplodingClient:
    """A paid client that fails the test if it is ever constructed-into-use."""

    def __call__(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("paid client invoked despite cost-cap abort")


class _GatedPaidStage(_CostStage):
    """A standalone paid stage whose projected cost pushes the total over cap.

    Its `next_call_cost_usd` is a full Veo clip ($3.20). If the dispatcher's
    gate is working, `run()` is never reached, so constructing/calling the paid
    client (which explodes) proves the gate fired BEFORE any paid call.
    """

    id: ClassVar[str] = "01_pick"
    requires: ClassVar[tuple[str, ...]] = ()
    _gate_cost: ClassVar[float] = VEO_FAST_CLIP_USD

    def run(self, project: Project) -> StageResult:  # pragma: no cover - gate blocks
        _ExplodingClient()()  # would raise; gate must prevent reaching this
        return super().run(project)


def _seed_accumulated_cost(projects_root: Path, slug: str, *, cost: float) -> None:
    """Write a prior `metrics.cost_usd` so the next paid stage approaches the cap.

    Records the cost on the `11_package` slot (any stage other than the one
    under test) by transitioning it to DONE with the metric, then saving.
    """
    project = Project.load(projects_root, slug)
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    # Legal path is pending → running → done (manifest state matrix).
    manifest = project.manifest.transition(
        "11_package", StageStatus.RUNNING, started_at=now
    )
    manifest = manifest.transition(
        "11_package",
        StageStatus.DONE,
        outputs=(),
        metrics={"cost_usd": cost},
        finished_at=now,
    )
    project.with_manifest(manifest).save_manifest()


def test_tc_23_1_cost_cap_aborts_before_paid_call(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher raises CostCapExceeded and the paid client is NOT called.

    Standard cap is $3.00. We seed $2.50 of prior cost, then dispatch a stage
    whose projected cost is +$3.20 → $5.70 > $3.00. The gate must abort with
    CostCapExceeded, mark the stage FAILED, and never reach the exploding
    client inside `run()`.
    """
    monkeypatch.setattr(_stages, "ALL_STAGES", (_GatedPaidStage,))
    _set_mode(monkeypatch, "standard")
    slug = _create_project(projects_root)
    _seed_accumulated_cost(projects_root, slug, cost=2.50)

    result = runner.invoke(cli.app, [*_root(projects_root), "pick", slug])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    manifest = Manifest.load(projects_root / slug / "manifest.json")
    rec = manifest.stages["01_pick"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "CostCapExceeded"
    # The gated stage recorded NO cost (cost is written only on DONE); the only
    # accumulated cost is the seeded prior $2.50 — no double-charge, no paid call.
    assert accumulated_cost_usd(manifest) == pytest.approx(2.50, abs=1e-6)


def test_tc_23_1_at_cap_boundary_allows_call(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: projected == cap is allowed (strict `>` gate).

    Seed $2.96 + a $0.04 Imagen call → exactly $3.00 == cap. The gate uses
    STRICT greater-than, so the stage proceeds and records its cost.
    """

    class _ImagenStage(_CostStage):
        id: ClassVar[str] = "01_pick"
        requires: ClassVar[tuple[str, ...]] = ()
        _cost: ClassVar[float] = IMAGEN_IMAGE_USD
        _gate_cost: ClassVar[float] = IMAGEN_IMAGE_USD

    monkeypatch.setattr(_stages, "ALL_STAGES", (_ImagenStage,))
    _set_mode(monkeypatch, "standard")
    slug = _create_project(projects_root)
    _seed_accumulated_cost(projects_root, slug, cost=2.96)

    result = runner.invoke(cli.app, [*_root(projects_root), "pick", slug])
    assert result.exit_code == 0, result.output

    manifest = Manifest.load(projects_root / slug / "manifest.json")
    assert manifest.stages["01_pick"].status == StageStatus.DONE
    # 2.96 seeded + 0.04 this stage == exactly the $3.00 cap.
    assert accumulated_cost_usd(manifest) == pytest.approx(_STANDARD_CAP, abs=1e-6)
