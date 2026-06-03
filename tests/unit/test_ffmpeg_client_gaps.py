"""Unit tests for ffmpeg_client.py coverage gaps.

Targets:
- check_available_or_raise: binary not found (line 50), subprocess error (lines 62-63),
  empty stdout fallback (line 64), stdout with content
- _ffmpeg_path: None branch (lines 69-70)
- frame_align_durations: fps <= 0 raises ValueError (lines 117-118)
- build_concat_file: empty list error (lines 173), length mismatch (175)
- assemble: non-zero exit → FfmpegAssembleFailed (lines 252-271)
- probe_video: N/A and empty fields → None (lines 397-415)
- probe_audio_duration_sec: empty stdout → None, "N/A" → None, ValueError → None (lines 446-451)
- _run_one: non-zero exit → FfmpegAssembleFailed (lines 651-652)
- _build_argv: smoke test returns a list (line 741)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shipcast.clients import ffmpeg_client as ff
from shipcast.errors import FfmpegAssembleFailed, FfmpegNotFound

# ---------------------------------------------------------------------------
# check_available_or_raise
# ---------------------------------------------------------------------------


def test_check_available_raises_ffmpeg_not_found_when_missing() -> None:
    with patch.object(ff, "_WHICH", return_value=None):
        with pytest.raises(FfmpegNotFound, match="not on PATH"):
            ff.check_available_or_raise()


def test_check_available_raises_ffmpeg_not_found_on_subprocess_error() -> None:
    with patch.object(ff, "_WHICH", return_value="/usr/bin/ffmpeg"):
        with patch(
            "subprocess.run",
            side_effect=subprocess.SubprocessError("boom"),
        ):
            with pytest.raises(FfmpegNotFound, match="ffmpeg invocation failed"):
                ff.check_available_or_raise()


def test_check_available_raises_ffmpeg_not_found_on_oserror() -> None:
    with patch.object(ff, "_WHICH", return_value="/usr/bin/ffmpeg"):
        with patch("subprocess.run", side_effect=OSError("no such file")):
            with pytest.raises(FfmpegNotFound):
                ff.check_available_or_raise()


def test_check_available_returns_first_stdout_line() -> None:
    mock_result = MagicMock()
    mock_result.stdout = "ffmpeg version 6.0\nMore stuff\n"
    with patch.object(ff, "_WHICH", return_value="/usr/bin/ffmpeg"):
        with patch("subprocess.run", return_value=mock_result):
            result = ff.check_available_or_raise()
    assert result == "ffmpeg version 6.0"


def test_check_available_returns_fallback_when_empty_stdout() -> None:
    mock_result = MagicMock()
    mock_result.stdout = ""
    with patch.object(ff, "_WHICH", return_value="/usr/bin/ffmpeg"):
        with patch("subprocess.run", return_value=mock_result):
            result = ff.check_available_or_raise()
    assert "unknown" in result.lower() or result == "ffmpeg (version unknown)"


# ---------------------------------------------------------------------------
# _ffmpeg_path: None branch
# ---------------------------------------------------------------------------


def test_ffmpeg_path_returns_none_when_not_found() -> None:
    with patch("shutil.which", return_value=None):
        result = ff._ffmpeg_path()
    assert result is None


def test_ffmpeg_path_returns_path_when_found() -> None:
    with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        result = ff._ffmpeg_path()
    assert result == Path("/usr/bin/ffmpeg")


# ---------------------------------------------------------------------------
# frame_align_durations: fps <= 0 raises ValueError
# ---------------------------------------------------------------------------


def test_frame_align_durations_zero_fps_raises() -> None:
    with pytest.raises(ValueError, match="fps must be positive"):
        ff.frame_align_durations([1.0, 2.0], fps=0)


def test_frame_align_durations_negative_fps_raises() -> None:
    with pytest.raises(ValueError, match="fps must be positive"):
        ff.frame_align_durations([1.0], fps=-1)


# ---------------------------------------------------------------------------
# build_concat_file: empty list and length mismatch
# ---------------------------------------------------------------------------


def test_build_concat_file_empty_list_raises() -> None:
    with pytest.raises(ValueError, match="at least one image"):
        ff.build_concat_file([], [])


def test_build_concat_file_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        ff.build_concat_file([Path("/a.jpg"), Path("/b.jpg")], [1.0])


# ---------------------------------------------------------------------------
# assemble: non-zero exit → FfmpegAssembleFailed
# ---------------------------------------------------------------------------


def test_assemble_raises_on_nonzero_exit(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "error from ffmpeg"
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(FfmpegAssembleFailed) as exc_info:
            ff.assemble(
                concat_path=tmp_path / "concat.txt",
                audio_path=tmp_path / "audio.mp3",
                output_path=tmp_path / "out.mp4",
                audio_duration_sec=10.0,
            )
    assert exc_info.value.returncode == 1
    assert "error from ffmpeg" in exc_info.value.stderr_tail


def test_assemble_succeeds_on_zero_exit(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "ok"
    mock_result.stderr = ""
    with patch("subprocess.run", return_value=mock_result):
        result = ff.assemble(
            concat_path=tmp_path / "concat.txt",
            audio_path=tmp_path / "audio.mp3",
            output_path=tmp_path / "out.mp4",
            audio_duration_sec=10.0,
        )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# probe_video: N/A and empty values → None
# ---------------------------------------------------------------------------


def test_probe_video_all_na_fields_return_none(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "codec_name=N/A\nwidth=N/A\nheight=N/A\nduration=N/A\n"
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        result = ff.probe_video(tmp_path / "video.mp4")
    assert result.codec_name is None
    assert result.width is None
    assert result.height is None
    assert result.duration_sec is None


def test_probe_video_empty_output_returns_none_fields(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = ""
    with patch("subprocess.run", return_value=mock_result):
        result = ff.probe_video(tmp_path / "video.mp4")
    assert result.codec_name is None
    assert result.width is None


def test_probe_video_non_integer_width_returns_none(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "codec_name=h264\nwidth=notanint\nheight=1920\nduration=4.0\n"
    with patch("subprocess.run", return_value=mock_result):
        result = ff.probe_video(tmp_path / "video.mp4")
    assert result.width is None
    assert result.codec_name == "h264"


def test_probe_video_non_float_duration_returns_none(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "codec_name=h264\nwidth=1080\nheight=1920\nduration=notafloat\n"
    with patch("subprocess.run", return_value=mock_result):
        result = ff.probe_video(tmp_path / "video.mp4")
    assert result.duration_sec is None


def test_probe_video_valid_fields(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "codec_name=h264\nwidth=1080\nheight=1920\nduration=4.5\n"
    with patch("subprocess.run", return_value=mock_result):
        result = ff.probe_video(tmp_path / "video.mp4")
    assert result.codec_name == "h264"
    assert result.width == 1080
    assert result.height == 1920
    assert result.duration_sec == pytest.approx(4.5)


# ---------------------------------------------------------------------------
# probe_audio_duration_sec: empty, N/A, ValueError branches
# ---------------------------------------------------------------------------


def test_probe_audio_duration_empty_stdout_returns_none(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = ""
    with patch("subprocess.run", return_value=mock_result):
        result = ff.probe_audio_duration_sec(tmp_path / "audio.mp3")
    assert result is None


def test_probe_audio_duration_na_returns_none(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "N/A\n"
    with patch("subprocess.run", return_value=mock_result):
        result = ff.probe_audio_duration_sec(tmp_path / "audio.mp3")
    assert result is None


def test_probe_audio_duration_invalid_float_returns_none(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "notafloat\n"
    with patch("subprocess.run", return_value=mock_result):
        result = ff.probe_audio_duration_sec(tmp_path / "audio.mp3")
    assert result is None


def test_probe_audio_duration_valid_float(tmp_path: Path) -> None:
    mock_result = MagicMock()
    mock_result.stdout = "12.345\n"
    with patch("subprocess.run", return_value=mock_result):
        result = ff.probe_audio_duration_sec(tmp_path / "audio.mp3")
    assert result == pytest.approx(12.345)


# ---------------------------------------------------------------------------
# _run_one: non-zero exit → FfmpegAssembleFailed
# ---------------------------------------------------------------------------


def test_run_one_raises_assemble_failed_on_nonzero() -> None:
    mock_result = MagicMock()
    mock_result.returncode = 2
    mock_result.stderr = "encode error"
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(FfmpegAssembleFailed) as exc_info:
            ff._run_one(["ffmpeg", "-y", "-version"])
    assert exc_info.value.returncode == 2


# ---------------------------------------------------------------------------
# _build_argv: smoke test — returns a list with key flags
# ---------------------------------------------------------------------------


def test_build_argv_contains_required_flags(tmp_path: Path) -> None:
    argv = ff._build_argv(
        concat_path=tmp_path / "c.txt",
        audio_path=tmp_path / "a.mp3",
        output_path=tmp_path / "out.mp4",
        audio_duration_sec=15.5,
    )
    joined = " ".join(argv)
    assert "ffmpeg" in argv
    assert "libx264" in joined
    assert "15.500" in joined
    assert "-f" in argv
    assert "mp4" in argv
