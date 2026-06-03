"""Unit tests for the ffmpeg Ken-Burns helper (Slice 13).

``ken_burns_clip`` turns one still PNG into a slow pan/zoom 1080x1920 h264 clip.
These tests shell out to the REAL ffmpeg/ffprobe (installed on dev machines,
v8.x). Clips are kept short (~3 s) and use ``-preset ultrafast`` so the suite
stays fast.
"""

from __future__ import annotations

import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from shipcast.clients import ffmpeg_client
from shipcast.errors import FfmpegAssembleFailed


def _make_png(path: Path, *, w: int = 120, h: int = 80) -> None:
    """Write a tiny solid-colour PNG without Pillow (keeps deps minimal)."""
    raw = b""
    for _ in range(h):
        raw += b"\x00" + bytes([180, 60, 40] * w)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(raw))
    png += _chunk(b"IEND", b"")
    path.write_bytes(png)


def _probe(path: Path) -> tuple[str, int, int, float]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fields: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k] = v
    return (
        fields["codec_name"],
        int(fields["width"]),
        int(fields["height"]),
        float(fields["duration"]),
    )


def test_ken_burns_clip_dims_codec_duration(tmp_path: Path) -> None:
    """A 3 s render → 1080x1920 h264 clip of ~3.0 s."""
    still = tmp_path / "still.png"
    _make_png(still)
    out = tmp_path / "beat_00.mp4"

    result = ffmpeg_client.ken_burns_clip(
        still_path=still, duration_sec=3.0, output_path=out, fast=True
    )
    assert result == out
    assert out.is_file()
    codec, width, height, duration = _probe(out)
    assert codec == "h264"
    assert (width, height) == (1080, 1920)
    assert abs(duration - 3.0) < 0.2


def test_ken_burns_clip_five_seconds(tmp_path: Path) -> None:
    """The upper bound of the 3-5 s window renders at the requested length."""
    still = tmp_path / "still.png"
    _make_png(still)
    out = tmp_path / "beat_01.mp4"
    ffmpeg_client.ken_burns_clip(
        still_path=still, duration_sec=5.0, output_path=out, fast=True
    )
    _, _, _, duration = _probe(out)
    assert abs(duration - 5.0) < 0.2


def test_ken_burns_clip_missing_input_raises(tmp_path: Path) -> None:
    """ffmpeg non-zero exit on a missing still surfaces FfmpegAssembleFailed."""
    out = tmp_path / "x.mp4"
    with pytest.raises(FfmpegAssembleFailed):
        ffmpeg_client.ken_burns_clip(
            still_path=tmp_path / "does-not-exist.png",
            duration_sec=3.0,
            output_path=out,
            fast=True,
        )
