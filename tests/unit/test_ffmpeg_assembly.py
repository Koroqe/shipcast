"""Unit tests for the Stage-08 ffmpeg argv builders (pure, no subprocess).

These assert the duck filter / BGM-presence branching (TC-11.7 / TC-11.8) and
the square-crop loop geometry without shelling out to ffmpeg. Real-ffmpeg
assembly is exercised in ``tests/integration/test_s08_video.py``.
"""

from __future__ import annotations

from pathlib import Path

from shipcast.clients import ffmpeg_client as ff


def test_audio_mix_no_bgm_has_no_duck_filter() -> None:
    argv = ff.build_audio_mix_argv(
        video_path=Path("/v.mp4"),
        narration_path=Path("/n.mp3"),
        output_path=Path("/out.mp4"),
        bgm_path=None,
    )
    joined = " ".join(argv)
    assert "filter_complex" not in joined
    assert "amix" not in joined
    assert "volume=" not in joined
    # Narration mapped as the sole audio track.
    assert "1:a:0" in argv


def test_audio_mix_with_bgm_ducks_3db_and_mixes() -> None:
    argv = ff.build_audio_mix_argv(
        video_path=Path("/v.mp4"),
        narration_path=Path("/n.mp3"),
        output_path=Path("/out.mp4"),
        bgm_path=Path("/music/track.mp3"),
    )
    joined = " ".join(argv)
    assert "-filter_complex" in argv
    assert "volume=-3.0dB" in joined
    assert "amix=inputs=2" in joined
    assert "/music/track.mp3" in argv


def test_loop_mp4_argv_square_crops_and_strips_audio() -> None:
    argv = ff.build_loop_mp4_argv(
        source_path=Path("/hero.mp4"), output_path=Path("/loop.mp4")
    )
    joined = " ".join(argv)
    assert f"crop={ff.SQUARE_SIZE}:{ff.SQUARE_SIZE}" in joined
    assert "-an" in argv
    assert f"{ff.LOOP_SECONDS:.3f}" in argv


def test_loop_gif_argv_uses_palette_pipeline() -> None:
    argv = ff.build_loop_gif_argv(
        source_path=Path("/loop.mp4"), output_path=Path("/loop.gif")
    )
    joined = " ".join(argv)
    assert "palettegen" in joined
    assert "paletteuse" in joined
    assert argv[-1] == "/loop.gif"


def test_concat_argv_is_video_only() -> None:
    argv = ff.build_concat_argv(
        concat_path=Path("/c.txt"), output_path=Path("/raw.mp4")
    )
    assert "-an" in argv
    assert "concat" in argv


def test_caption_overlay_argv_maps_overlay_and_keeps_audio() -> None:
    argv = ff.build_caption_overlay_argv(
        video_path=Path("/v.mp4"),
        frames_glob="/frames/f_%05d.png",
        fps=30,
        output_path=Path("/cap.mp4"),
    )
    joined = " ".join(argv)
    assert "overlay=shortest=1" in joined
    assert "0:a?" in argv
