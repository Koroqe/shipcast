"""Unit tests for `s08_video` caption-mode + BGM resolution (no ffmpeg).

These exercise the stage's pure helpers - caption-mode reading from
``03_brand/voice.md`` (TC-11.2..11.6, TC-11.10) and the first-alphabetical BGM
selection (TC-11.7 / TC-11.8) - without assembling any video.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from shipcast.config import Settings
from shipcast.paths import default_template_path
from shipcast.project import Project
from shipcast.stages.s08_video import VideoStage


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[..., Project]:
    def _make(voice_md: str | None) -> Project:
        root = tmp_path / "projects"
        root.mkdir(exist_ok=True)
        proj = Project.create(
            root, "entry", {}, settings=Settings(),
            template_path=default_template_path(),
        )
        proj.input_path.write_text(
            "repo_path: /tmp\nentry_heading: X\nbrand_slug: test-brand\n",
            encoding="utf-8",
        )
        if voice_md is not None:
            brand = proj.stage_dir("03_brand")
            brand.mkdir(parents=True, exist_ok=True)
            (brand / "voice.md").write_text(voice_md, encoding="utf-8")
        return proj

    return _make


# --- caption-mode resolution (reads 03_brand/voice.md) --------------------- #


def test_tc_11_2_chip(make_project: Callable[..., Project]) -> None:
    proj = make_project("caption_mode: chip\n")
    assert VideoStage()._resolve_caption_mode(proj) == "chip"


def test_tc_11_3_karaoke(make_project: Callable[..., Project]) -> None:
    proj = make_project("tone: bold\ncaption_mode: karaoke\n")
    assert VideoStage()._resolve_caption_mode(proj) == "karaoke"


def test_tc_11_4_reveal(make_project: Callable[..., Project]) -> None:
    proj = make_project("caption_mode: reveal\n")
    assert VideoStage()._resolve_caption_mode(proj) == "reveal"


def test_tc_11_5_absent_defaults_chip(make_project: Callable[..., Project]) -> None:
    proj = make_project("tone: warm\nno mode here\n")
    assert VideoStage()._resolve_caption_mode(proj) == "chip"


def test_tc_11_6_unrecognized_defaults_chip(make_project: Callable[..., Project]) -> None:
    proj = make_project("caption_mode: fancytype\n")
    assert VideoStage()._resolve_caption_mode(proj) == "chip"


def test_tc_11_10_reads_from_03_brand_path_no_error(
    make_project: Callable[..., Project],
) -> None:
    # voice.md ONLY at 03_brand/voice.md (Finding-1 canonical path); the stage
    # must read it without FileNotFoundError.
    proj = make_project("caption_mode: karaoke\n")
    assert VideoStage()._resolve_caption_mode(proj) == "karaoke"


def test_caption_mode_missing_voice_md_defaults_chip(
    make_project: Callable[..., Project],
) -> None:
    proj = make_project(None)  # no voice.md at all
    assert VideoStage()._resolve_caption_mode(proj) == "chip"


# --- BGM selection (first-alphabetical from _brand/<slug>/music/) ----------- #


def test_tc_11_7_no_music_returns_none(make_project: Callable[..., Project]) -> None:
    proj = make_project("caption_mode: chip\n")
    assert VideoStage()._resolve_bgm(proj) is None


def test_tc_11_8_first_alphabetical_music_selected(
    make_project: Callable[..., Project],
) -> None:
    proj = make_project("caption_mode: chip\n")
    music = proj.root / "_brand" / "test-brand" / "music"
    music.mkdir(parents=True, exist_ok=True)
    (music / "zeta.mp3").write_bytes(b"\x00")
    (music / "alpha.mp3").write_bytes(b"\x00")
    (music / "mid.wav").write_bytes(b"\x00")
    chosen = VideoStage()._resolve_bgm(proj)
    assert chosen is not None
    assert chosen.name == "alpha.mp3"
