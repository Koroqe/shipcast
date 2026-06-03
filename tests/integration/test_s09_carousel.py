"""Integration tests for the `s09_graphics` LinkedIn carousel (Slice 18).

Owned TCs (Section 12, Slice 18):
- TC-12.8: the carousel is EXACTLY 6 slides, each 1080x1350; the composer
  receives the channel hook text for slide 01 and a CTA for slide 06; slides
  02-05 carry the four ``carousel_beats`` headlines.
- TC-12.9 (joint, Slices 16-18): both flags false → exactly 11 files (4 aspect
  cards + OG + 6 carousel slides).

The carousel is ALWAYS rendered (independent of the two conditional flags). The
composer (`shipcast.marketing.carousel`) is pure PIL — no external API — so the
6 slides render deterministically. Gemini Imagen is mocked (it backs only the
aspect/OG/stat cards, never the carousel).

Reuses the project builder + stub Gemini from ``test_s09_graphics`` so the
upstream seeding (01_pick + 03_brand + 04_plan done+approved) is not duplicated.
"""

from __future__ import annotations

from pathlib import Path

from shipcast.manifest import StageStatus
from shipcast.marketing import carousel as carousel_mod
from tests.integration.test_s09_graphics import (
    _PALETTE,
    _build_project,
    _run,
    _StubGemini,
)
from tests.unit.test_palette_conformance import assert_palette_conformance

_CAROUSEL_DIM = (1080, 1350)
_SLIDES = tuple(f"slide_{i:02d}.png" for i in range(1, 7))


# --------------------------------------------------------------------------- #
# TC-12.8 — exactly 6 carousel slides, each 1080x1350
# --------------------------------------------------------------------------- #


def test_tc_12_8_carousel_has_exactly_six_slides_at_dims(tmp_path: Path) -> None:
    from PIL import Image

    proj = _build_project(tmp_path, brief_fixture="both_false.json")
    result = _run(proj, _StubGemini())
    assert result.status == StageStatus.DONE

    carousel_dir = proj.stage_dir("09_graphics") / "carousel"
    pngs = sorted(p.name for p in carousel_dir.glob("*.png"))
    assert pngs == list(_SLIDES), f"expected exactly 6 slides, got {pngs}"

    for name in _SLIDES:
        with Image.open(carousel_dir / name) as img:
            assert img.size == _CAROUSEL_DIM, f"{name} is {img.size}"

    rels = {str(p) for p in result.outputs}
    for name in _SLIDES:
        assert f"09_graphics/carousel/{name}" in rels


def test_tc_12_8_composer_receives_hook_for_slide01_and_cta_for_slide06(
    tmp_path: Path,
) -> None:
    """The composer is invoked with the hook text (slide 01) + CTA (slide 06)."""
    proj = _build_project(tmp_path, brief_fixture="both_false.json")

    seen: list[tuple[int, str, str]] = []
    orig = carousel_mod.render_slide

    def _spy(
        idx: int,
        *,
        kind: str,
        headline: str,
        body: str,
        palette: tuple[str, str, str],
        font_path: Path | None,
        out_path: Path,
    ) -> None:
        seen.append((idx, kind, headline))
        orig(
            idx,
            kind=kind,
            headline=headline,
            body=body,
            palette=palette,
            font_path=font_path,
            out_path=out_path,
        )

    carousel_mod.render_slide = _spy  # type: ignore[assignment]
    try:
        result = _run(proj, _StubGemini())
    finally:
        carousel_mod.render_slide = orig  # type: ignore[assignment]

    assert result.status == StageStatus.DONE
    assert len(seen) == 6
    # slide 01 = hook, slide 06 = CTA.
    assert seen[0][0] == 1 and seen[0][1] == "hook"
    assert seen[5][0] == 6 and seen[5][1] == "cta"
    # slides 02-05 carry the four carousel-beat headlines, in order.
    beat_headlines = [s[2] for s in seen[1:5]]
    assert beat_headlines == [f"Beat {i} headline" for i in range(4)]
    # slide 06 headline is the brief CTA.
    assert seen[5][2] == "Try it today"


def test_carousel_slides_palette_conformance(tmp_path: Path) -> None:
    proj = _build_project(tmp_path, brief_fixture="both_false.json")
    _run(proj, _StubGemini())
    carousel_dir = proj.stage_dir("09_graphics") / "carousel"
    for name in _SLIDES:
        assert_palette_conformance(carousel_dir / name, _PALETTE)


# --------------------------------------------------------------------------- #
# TC-12.9 — both flags false → exactly 11 files
# --------------------------------------------------------------------------- #


def test_tc_12_9_minimum_output_set_both_flags_false(tmp_path: Path) -> None:
    proj = _build_project(tmp_path, brief_fixture="both_false.json")
    result = _run(proj, _StubGemini())
    assert result.status == StageStatus.DONE

    rels = {str(p) for p in result.outputs}
    expected = (
        {
            "09_graphics/1x1.png",
            "09_graphics/16x9.png",
            "09_graphics/9x16.png",
            "09_graphics/4x5.png",
            "09_graphics/og_card.png",
        }
        | {f"09_graphics/carousel/{name}" for name in _SLIDES}
    )
    assert rels == expected
    assert len(rels) == 11

    # No stat or code files declared or on disk.
    assert not any(r.startswith("09_graphics/stat_") for r in rels)
    assert "09_graphics/code.png" not in rels
    out = proj.stage_dir("09_graphics")
    assert not (out / "code.png").exists()
    assert sorted(p.name for p in out.glob("stat_*.png")) == []


def test_carousel_renders_pure_no_imagen_call_count_unchanged(tmp_path: Path) -> None:
    """The carousel must not call Imagen — only the 5 cards (4 aspect + OG) do."""
    proj = _build_project(tmp_path, brief_fixture="both_false.json")
    gemini = _StubGemini()
    _run(proj, gemini)
    # 4 aspect + 1 OG = 5 Imagen calls; carousel is pure PIL.
    assert gemini.calls == 5
