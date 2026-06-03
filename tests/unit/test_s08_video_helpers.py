"""Unit tests for `s08_video` helper + validation branches (no ffmpeg).

Covers the input-loading guards, palette fallback, brand-font resolution, and
``validate_outputs`` magic-byte checks — the error paths the integration test
does not exercise.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from shipcast.config import Settings
from shipcast.errors import StageInputMissing, StageOutputInvalid
from shipcast.manifest import StageStatus, dump_json_canonical
from shipcast.paths import default_template_path
from shipcast.project import Project
from shipcast.stage import StageResult
from shipcast.stages.s08_video import VideoStage

# A minimal valid 32-byte MP4 head carrying an ftyp box.
_MP4_HEAD = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 8
_GIF_HEAD = b"GIF89a" + b"\x00" * 16


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[..., Project]:
    def _make(brand_slug: str | None = "test-brand") -> Project:
        root = tmp_path / "projects"
        root.mkdir(exist_ok=True)
        proj = Project.create(
            root, "entry", {}, settings=Settings(),
            template_path=default_template_path(),
        )
        body = "repo_path: /tmp\nentry_heading: X\n"
        if brand_slug is not None:
            body += f"brand_slug: {brand_slug}\n"
        proj.input_path.write_text(body, encoding="utf-8")
        return proj

    return _make


# --- _load_clip_paths / _load_words guards --------------------------------- #


def test_load_clip_paths_missing_clips_json(make_project: Callable[..., Project]) -> None:
    proj = make_project()
    with pytest.raises(StageInputMissing):
        VideoStage()._load_clip_paths(proj)


def test_load_clip_paths_missing_clip_file(make_project: Callable[..., Project]) -> None:
    proj = make_project()
    va = proj.stage_dir("06_video_assets")
    va.mkdir(parents=True, exist_ok=True)
    (va / "clips.json").write_text(
        dump_json_canonical(
            {"mode": "standard",
             "clips": [{"index": 0, "filename": "beat_00.mp4",
                        "source": "ken_burns", "duration_sec": 4.0}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(StageInputMissing):
        VideoStage()._load_clip_paths(proj)


def test_load_words_missing_file(make_project: Callable[..., Project]) -> None:
    proj = make_project()
    with pytest.raises(StageInputMissing):
        VideoStage()._load_words(proj)


def test_load_words_not_a_list(make_project: Callable[..., Project]) -> None:
    proj = make_project()
    voice = proj.stage_dir("07_voice")
    voice.mkdir(parents=True, exist_ok=True)
    (voice / "words.json").write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(StageInputMissing):
        VideoStage()._load_words(proj)


def test_load_words_keeps_three_keys(make_project: Callable[..., Project]) -> None:
    proj = make_project()
    voice = proj.stage_dir("07_voice")
    voice.mkdir(parents=True, exist_ok=True)
    (voice / "words.json").write_text(
        dump_json_canonical(
            [{"word": "hi", "start_sec": 0.0, "end_sec": 0.3, "confidence": 0.9}]
        ),
        encoding="utf-8",
    )
    words = VideoStage()._load_words(proj)
    assert words == [{"word": "hi", "start_sec": 0.0, "end_sec": 0.3}]


# --- palette + font resolution --------------------------------------------- #


def test_resolve_palette_fallback_when_no_proposal(
    make_project: Callable[..., Project],
) -> None:
    proj = make_project()
    palette = VideoStage()._resolve_palette(proj)
    # active = (accent, neutral); inactive = (primary, neutral) from fallback.
    assert palette["active"][0] == "#FF6B6B"
    assert palette["inactive"][0] == "#1D2A41"


def test_resolve_palette_uses_proposal_hexes(
    make_project: Callable[..., Project],
) -> None:
    proj = make_project()
    brand = proj.stage_dir("03_brand")
    brand.mkdir(parents=True, exist_ok=True)
    (brand / "proposal.json").write_text(
        dump_json_canonical(
            {"palette": ["#111111", "#222222", "#333333"],
             "font_family": "Inter", "logo_detected": True}
        ),
        encoding="utf-8",
    )
    palette = VideoStage()._resolve_palette(proj)
    assert palette["inactive"][0] == "#111111"
    assert palette["active"][0] == "#222222"
    assert palette["active"][1] == "#333333"


def test_brand_font_path_none_when_absent(
    make_project: Callable[..., Project],
) -> None:
    proj = make_project()
    assert VideoStage()._brand_font_path(proj) is None


def test_brand_font_path_picks_first_ttf(make_project: Callable[..., Project]) -> None:
    proj = make_project()
    fonts = proj.root / "_brand" / "test-brand" / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    (fonts / "Zeta.ttf").write_bytes(b"x")
    (fonts / "Alpha.ttf").write_bytes(b"x")
    chosen = VideoStage()._brand_font_path(proj)
    assert chosen is not None and chosen.name == "Alpha.ttf"


def test_read_brand_slug_missing_input_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    proj = Project.create(
        root, "entry", {}, settings=Settings(),
        template_path=default_template_path(),
    )
    proj.input_path.unlink(missing_ok=True)
    assert VideoStage._read_brand_slug(proj) is None
    assert VideoStage()._resolve_bgm(proj) is None
    assert VideoStage()._brand_font_path(proj) is None


# --- validate_outputs magic-byte checks ------------------------------------ #


def _seed_outputs(
    proj: Project, *, showcase: bytes, loop: bytes, gif: bytes
) -> StageResult:
    out = proj.stage_dir("08_video")
    out.mkdir(parents=True, exist_ok=True)
    (out / "showcase.mp4").write_bytes(showcase)
    (out / "loop_6s.mp4").write_bytes(loop)
    (out / "loop_6s.gif").write_bytes(gif)
    return StageResult(
        status=StageStatus.DONE,
        outputs=(
            Path("08_video/showcase.mp4"),
            Path("08_video/loop_6s.mp4"),
            Path("08_video/loop_6s.gif"),
        ),
    )


def test_validate_outputs_accepts_valid_headers(
    make_project: Callable[..., Project],
) -> None:
    proj = make_project()
    result = _seed_outputs(proj, showcase=_MP4_HEAD, loop=_MP4_HEAD, gif=_GIF_HEAD)
    VideoStage().validate_outputs(proj, result)  # no raise


def test_validate_outputs_rejects_bad_mp4(
    make_project: Callable[..., Project],
) -> None:
    proj = make_project()
    result = _seed_outputs(proj, showcase=b"NOTMP4" * 6, loop=_MP4_HEAD, gif=_GIF_HEAD)
    with pytest.raises(StageOutputInvalid):
        VideoStage().validate_outputs(proj, result)


def test_validate_outputs_rejects_bad_gif(
    make_project: Callable[..., Project],
) -> None:
    proj = make_project()
    result = _seed_outputs(proj, showcase=_MP4_HEAD, loop=_MP4_HEAD, gif=b"NOTAGIF")
    with pytest.raises(StageOutputInvalid):
        VideoStage().validate_outputs(proj, result)
