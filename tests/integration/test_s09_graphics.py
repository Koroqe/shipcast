"""Integration + unit tests for `s09_graphics` Slice 16 — aspect cards.

Owned TCs (Section 12):
- TC-12.2 (partial): the 4 aspect cards open at their exact canonical dims
  (1x1=1080x1080, 16x9=1920x1080, 9x16=1080x1920, 4x5=1080x1350).
- TC-12.3: each of the 4 aspect cards passes the delta-E CIE2000
  palette-conformance gate (quantize-5, >= 80 % within dE<10 of the 5 refs).
- TC-12.10: `GeminiRateLimited` on the 2nd Imagen call → stage `failed`.

Gemini Imagen is ALWAYS mocked: the mock returns a SOLID brand-colour PNG at the
requested aspect ratio (a real, decodable PNG) so the card render is exercised
end-to-end (resize + headline overlay) and the palette-conformance gate has a
deterministic on-brand background. PIL is real.

The reusable `assert_palette_conformance` helper is imported from
`tests/unit/test_palette_conformance.py` (testing rule) and called on every card.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from shipcast.composition.color import hex_to_rgb
from shipcast.config import Settings
from shipcast.errors import GeminiRateLimited
from shipcast.manifest import Manifest, StageStatus, dump_json_canonical
from shipcast.paths import default_template_path
from shipcast.project import Project
from shipcast.stages.s09_graphics import GraphicsStage

# Import the reusable conformance helper from the unit test module (its home).
from tests.unit.test_palette_conformance import assert_palette_conformance

# --------------------------------------------------------------------------- #
# Pinned brand palette + brief fixtures (NOT live LLM output)
# --------------------------------------------------------------------------- #

_PRIMARY = "#1D2A41"  # deep navy
_ACCENT = "#FF6B6B"  # coral
_NEUTRAL = "#F4F1DE"  # cream
_PALETTE = (_PRIMARY, _ACCENT, _NEUTRAL)

_CARD_DIMS = {
    "1x1.png": (1080, 1080),
    "16x9.png": (1920, 1080),
    "9x16.png": (1080, 1920),
    "4x5.png": (1080, 1350),
}


def _pinned_brief() -> dict[str, Any]:
    return {
        "hook_template_per_channel": {
            "x": "we_just_shipped",
            "linkedin": "before_after",
            "blog": "problem_aha",
        },
        "ctas": ["Try it today"],
        "video_beats": [
            {"image_prompt": f"beat {i}", "narration": f"line {i}", "duration_sec": 4.0}
            for i in range(4)
        ],
        "carousel_beats": [
            {"headline": f"Beat {i} headline", "body": ""} for i in range(4)
        ],
        "has_stat_card": False,
        "has_code_screenshot": False,
    }


def _solid_png_bytes(rgb: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="PNG")
    return buf.getvalue()


class _StubGemini:
    """Mock Gemini that returns a solid brand-PRIMARY PNG at the requested ratio."""

    def __init__(self, *, rate_limit_after: int | None = None) -> None:
        self.calls = 0
        self._rate_limit_after = rate_limit_after
        self.ratios_seen: list[str] = []

    def generate_image(
        self,
        prompt: str,
        *,
        model: str,
        seed: int,
        reference_image_bytes: bytes | None = None,
        aspect_ratio: str = "16:9",
    ) -> bytes:
        self.calls += 1
        self.ratios_seen.append(aspect_ratio)
        if self._rate_limit_after is not None and self.calls > self._rate_limit_after:
            raise GeminiRateLimited("HTTP 429 rate limited")
        # Return the still at the Gemini-native size for the ratio (the stage
        # normalises to the canonical card dims, so it need not match exactly).
        from shipcast.clients.gemini_client import ASPECT_RATIO_DIMENSIONS

        size = ASPECT_RATIO_DIMENSIONS[aspect_ratio]  # type: ignore[index]
        return _solid_png_bytes(hex_to_rgb(_PRIMARY), size)


class _Bundle:
    def __init__(self, gemini: _StubGemini) -> None:
        self.gemini = gemini


# --------------------------------------------------------------------------- #
# Project builder (seeds 01_pick + 03_brand + 04_plan done+approved)
# --------------------------------------------------------------------------- #


#: Pinned brief fixtures (NOT live LLM output) — drive deterministic
#: stat-card conditionality (Slice 17).
_BRIEFS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "briefs"


def _build_project(tmp_path: Path, *, brief_fixture: str | None = None) -> Project:
    root = tmp_path / "projects"
    root.mkdir()
    proj = Project.create(
        root,
        "entry",
        {},
        settings=Settings(),
        template_path=default_template_path(),
    )
    proj.input_path.write_text(
        "repo_path: /tmp\nentry_heading: X\nbrand_slug: test-brand\n", encoding="utf-8"
    )

    # 01_pick/entry.json — supplies the headline.
    pick_dir = proj.stage_dir("01_pick")
    pick_dir.mkdir(parents=True, exist_ok=True)
    (pick_dir / "entry.json").write_text(
        dump_json_canonical(
            {"name": "Add CSV export", "date": "2026-06-02", "summary": "", "details": ""}
        ),
        encoding="utf-8",
    )

    # 03_brand/proposal.json — supplies the 3-colour palette.
    brand_dir = proj.stage_dir("03_brand")
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "proposal.json").write_text(
        dump_json_canonical(
            {"palette": list(_PALETTE), "font_family": "Inter", "logo_detected": True}
        ),
        encoding="utf-8",
    )
    (brand_dir / "voice.md").write_text("caption_mode: chip\n", encoding="utf-8")

    # 04_plan/brief.json — pinned brief. A named fixture (NOT live LLM output)
    # overrides the inline default so stat-card conditionality is deterministic.
    plan_dir = proj.stage_dir("04_plan")
    plan_dir.mkdir(parents=True, exist_ok=True)
    if brief_fixture is not None:
        brief_text = (_BRIEFS_DIR / brief_fixture).read_text(encoding="utf-8")
    else:
        brief_text = dump_json_canonical(_pinned_brief())
    (plan_dir / "brief.json").write_text(brief_text, encoding="utf-8")

    # Brand pack fonts dir (no real .ttf — exercises the system-font fallback).
    fonts = root / "_brand" / "test-brand" / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)

    # Mark upstream done + approved.
    m = Manifest.load(proj.manifest_path)
    for sid, outs in (
        ("01_pick", ("01_pick/entry.json",)),
        ("03_brand", ("03_brand/proposal.json", "03_brand/voice.md")),
        ("04_plan", ("04_plan/brief.json",)),
    ):
        m = m.transition(sid, StageStatus.RUNNING)
        m = m.transition(sid, StageStatus.DONE, outputs=outs)
        m = m.approve(sid)
    m.save(proj.manifest_path)
    return Project.load(root, "entry")


def _run(proj: Project, gemini: _StubGemini) -> Any:
    stage = GraphicsStage(clients_factory=lambda _p: _Bundle(gemini))
    return stage.run(proj)


# --------------------------------------------------------------------------- #
# TC-12.2 (partial) — the 4 aspect cards exist at exact canonical dims
# --------------------------------------------------------------------------- #


def test_tc_12_2_aspect_card_dimensions(tmp_path: Path) -> None:
    from PIL import Image

    proj = _build_project(tmp_path)
    gemini = _StubGemini()
    result = _run(proj, gemini)
    assert result.status == StageStatus.DONE

    out = proj.stage_dir("09_graphics")
    rels = {str(p) for p in result.outputs}
    # The 4 aspect cards, the always-on OG card (Slice 17), and the always-on
    # 6-slide LinkedIn carousel (Slice 18). The default inline brief has
    # has_stat_card=False and has_code_screenshot=False, so no stat_*/code files.
    assert rels == (
        {f"09_graphics/{name}" for name in _CARD_DIMS}
        | {"09_graphics/og_card.png"}
        | {f"09_graphics/carousel/slide_{i:02d}.png" for i in range(1, 7)}
    )

    for name, dims in _CARD_DIMS.items():
        card = out / name
        assert card.is_file(), f"{name} missing"
        with Image.open(card) as img:
            assert img.size == dims, f"{name} is {img.size}, expected {dims}"

    # One Imagen call per aspect card plus one for the OG card.
    assert gemini.calls == 5
    assert gemini.ratios_seen == ["1:1", "16:9", "9:16", "4:5", "og"]


# --------------------------------------------------------------------------- #
# TC-12.3 — each aspect card passes ΔE-CIE2000 palette conformance
# --------------------------------------------------------------------------- #


def test_tc_12_3_aspect_cards_palette_conformance(tmp_path: Path) -> None:
    proj = _build_project(tmp_path)
    _run(proj, _StubGemini())
    out = proj.stage_dir("09_graphics")
    for name in _CARD_DIMS:
        assert_palette_conformance(out / name, _PALETTE)


# --------------------------------------------------------------------------- #
# TC-12.10 — GeminiRateLimited on the 2nd call → stage failure
# --------------------------------------------------------------------------- #


def test_tc_12_10_rate_limited_second_call_fails(tmp_path: Path) -> None:
    proj = _build_project(tmp_path)
    gemini = _StubGemini(rate_limit_after=1)
    with pytest.raises(GeminiRateLimited):
        _run(proj, gemini)
    # Exactly two calls made: the 1st succeeds, the 2nd raises.
    assert gemini.calls == 2


# --------------------------------------------------------------------------- #
# Cost + metrics
# --------------------------------------------------------------------------- #


def test_metrics_record_imagen_calls(tmp_path: Path) -> None:
    proj = _build_project(tmp_path)
    result = _run(proj, _StubGemini())
    # 4 aspect cards + 1 OG card = 5 Imagen stills @ $0.04 = $0.20.
    assert result.metrics["cost_usd"] == pytest.approx(0.20)
    assert result.metrics["cards"] == 5


def test_next_call_cost_is_one_imagen(tmp_path: Path) -> None:
    proj = _build_project(tmp_path)
    stage = GraphicsStage()
    assert stage.next_call_cost_usd(proj) == pytest.approx(0.04)


# --------------------------------------------------------------------------- #
# validate_outputs accepts the produced PNGs
# --------------------------------------------------------------------------- #


def test_validate_outputs_accepts_cards(tmp_path: Path) -> None:
    proj = _build_project(tmp_path)
    gemini = _StubGemini()
    result = _run(proj, gemini)
    stage = GraphicsStage()
    stage.validate_outputs(proj, result)  # must not raise


# --------------------------------------------------------------------------- #
# Slice 17 — OG card (always) + conditional stat card
# --------------------------------------------------------------------------- #

_STAT_DIMS = {
    "stat_1x1.png": (1080, 1080),
    "stat_16x9.png": (1920, 1080),
    "stat_9x16.png": (1080, 1920),
    "stat_4x5.png": (1080, 1350),
}


# --------------------------------------------------------------------------- #
# TC-12.2 (partial) / AC-3.7 — the OG card opens at exactly 1200x630
# --------------------------------------------------------------------------- #


def test_tc_12_2_og_card_dimensions(tmp_path: Path) -> None:
    from PIL import Image

    proj = _build_project(tmp_path)
    result = _run(proj, _StubGemini())
    assert result.status == StageStatus.DONE

    og = proj.stage_dir("09_graphics") / "og_card.png"
    assert og.is_file(), "og_card.png missing"
    with Image.open(og) as img:
        assert img.size == (1200, 630), f"og_card.png is {img.size}"

    rels = {str(p) for p in result.outputs}
    assert "09_graphics/og_card.png" in rels


def test_og_card_palette_conformance(tmp_path: Path) -> None:
    proj = _build_project(tmp_path)
    _run(proj, _StubGemini())
    og = proj.stage_dir("09_graphics") / "og_card.png"
    assert_palette_conformance(og, _PALETTE)


# --------------------------------------------------------------------------- #
# TC-12.4 — stat card rendered (4 ratios) when has_stat_card=true
# --------------------------------------------------------------------------- #


def test_tc_12_4_stat_cards_rendered_when_flag_true(tmp_path: Path) -> None:
    from PIL import Image

    proj = _build_project(tmp_path, brief_fixture="stat_true.json")
    result = _run(proj, _StubGemini())
    assert result.status == StageStatus.DONE

    out = proj.stage_dir("09_graphics")
    rels = {str(p) for p in result.outputs}

    for name, dims in _STAT_DIMS.items():
        card = out / name
        assert card.is_file(), f"{name} missing"
        with Image.open(card) as img:
            assert img.size == dims, f"{name} is {img.size}, expected {dims}"
        # Declared in the stage result so outputs-hash + reset cover them.
        assert f"09_graphics/{name}" in rels


def test_tc_12_4_stat_cards_palette_conformance(tmp_path: Path) -> None:
    proj = _build_project(tmp_path, brief_fixture="stat_true.json")
    _run(proj, _StubGemini())
    out = proj.stage_dir("09_graphics")
    for name in _STAT_DIMS:
        assert_palette_conformance(out / name, _PALETTE)


# --------------------------------------------------------------------------- #
# TC-12.5 — NO stat files when has_stat_card=false; _render_stat not called
# --------------------------------------------------------------------------- #


def test_tc_12_5_no_stat_files_when_flag_false(tmp_path: Path) -> None:
    proj = _build_project(tmp_path, brief_fixture="stat_false.json")

    # Spy on _render_stat so we can assert it is never invoked.
    stat_calls: list[Any] = []
    orig_render_stat = GraphicsStage._render_stat

    def _spy_render_stat(self: GraphicsStage, *args: Any, **kwargs: Any) -> None:
        stat_calls.append((args, kwargs))
        orig_render_stat(self, *args, **kwargs)

    stage = GraphicsStage(clients_factory=lambda _p: _Bundle(_StubGemini()))
    stage._render_stat = _spy_render_stat.__get__(stage, GraphicsStage)  # type: ignore[method-assign]
    result = stage.run(proj)
    assert result.status == StageStatus.DONE

    out = proj.stage_dir("09_graphics")
    # No stat_* files on disk.
    assert sorted(p.name for p in out.glob("stat_*.png")) == []
    # No stat_* paths declared in the result outputs.
    rels = {str(p) for p in result.outputs}
    assert not any(r.startswith("09_graphics/stat_") for r in rels)
    # OG card still present.
    assert (out / "og_card.png").is_file()
    assert "09_graphics/og_card.png" in rels
    # _render_stat was NOT called.
    assert stat_calls == []


# --------------------------------------------------------------------------- #
# Slice 18 — conditional code screenshot (Pygments + PIL, no external API)
# --------------------------------------------------------------------------- #


def _write_entry_with_code(proj: Project) -> None:
    """Overwrite ``01_pick/entry.json`` with a fenced code block in ``details``."""
    pick = proj.stage_dir("01_pick") / "entry.json"
    pick.write_text(
        dump_json_canonical(
            {
                "name": "Add CSV export",
                "date": "2026-06-02",
                "summary": "Spreadsheet export",
                "details": (
                    "We added a streaming export endpoint.\n\n"
                    "```python\n"
                    "def export(report_id: str) -> Iterator[str]:\n"
                    "    for row in fetch(report_id):\n"
                    "        yield to_csv(row)\n"
                    "```\n"
                ),
            }
        ),
        encoding="utf-8",
    )


def test_tc_12_6_code_png_rendered_when_flag_true(tmp_path: Path) -> None:
    """TC-12.6: has_code_screenshot=true → openable code.png, no external API."""
    from PIL import Image

    proj = _build_project(tmp_path, brief_fixture="code_true.json")
    _write_entry_with_code(proj)
    gemini = _StubGemini()
    result = _run(proj, gemini)
    assert result.status == StageStatus.DONE

    code = proj.stage_dir("09_graphics") / "code.png"
    assert code.is_file(), "code.png missing when has_code_screenshot=true"
    with Image.open(code) as img:
        assert img.format == "PNG"
        w, h = img.size
        assert w >= 400 and h >= 120
        # Real syntax-highlight content — not a flat fill.
        colors = img.convert("RGB").getcolors(maxcolors=1 << 16)
    assert colors is None or len(colors) > 8

    rels = {str(p) for p in result.outputs}
    assert "09_graphics/code.png" in rels
    # The code screenshot is rendered locally — Imagen is only called for the 5
    # cards (4 aspect + OG), never for the code screenshot.
    assert gemini.calls == 5


def test_tc_12_7_no_code_png_when_flag_false(tmp_path: Path) -> None:
    """TC-12.7: has_code_screenshot=false → no code.png; _render_code not called."""
    proj = _build_project(tmp_path, brief_fixture="code_false.json")

    code_calls: list[Any] = []
    orig_render_code = GraphicsStage._render_code

    def _spy_render_code(self: GraphicsStage, *args: Any, **kwargs: Any) -> None:
        code_calls.append((args, kwargs))
        orig_render_code(self, *args, **kwargs)

    stage = GraphicsStage(clients_factory=lambda _p: _Bundle(_StubGemini()))
    stage._render_code = _spy_render_code.__get__(stage, GraphicsStage)  # type: ignore[method-assign]
    result = stage.run(proj)
    assert result.status == StageStatus.DONE

    out = proj.stage_dir("09_graphics")
    assert not (out / "code.png").exists()
    rels = {str(p) for p in result.outputs}
    assert "09_graphics/code.png" not in rels
    assert code_calls == []


def test_code_png_renders_without_fenced_block(tmp_path: Path) -> None:
    """has_code_screenshot=true but no fenced block → synthesized snippet PNG."""
    from PIL import Image

    proj = _build_project(tmp_path, brief_fixture="code_true.json")
    # The default entry (written by _build_project) has empty details, so the
    # stage must synthesize a representative snippet rather than crash.
    result = _run(proj, _StubGemini())
    assert result.status == StageStatus.DONE
    code = proj.stage_dir("09_graphics") / "code.png"
    assert code.is_file()
    with Image.open(code) as img:
        assert img.format == "PNG"
