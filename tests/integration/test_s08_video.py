"""Integration tests for `s08_video` (Slice 15) - full video assembly.

Uses the REAL ffmpeg/ffprobe (v8.x on dev machines). To keep the suite fast we
generate small synthetic 1080x1920 clips via ``testsrc`` with
``-preset ultrafast``, a short sine-wave narration, and a canned ``words.json``.
No external API is touched.

Owned TCs (Section 11):
- TC-11.1: happy path -> showcase.mp4 (1080x1920, h264+aac, 15-25 s),
           loop_6s.mp4 (1080x1080, no audio, 6.0±0.1 s), loop_6s.gif (<= 8 MB).
- TC-11.2..11.6, TC-11.10: caption-mode resolution (unit-level, below + in
           test_captions_modes.py / test_s08_caption_mode.py).
- TC-11.9: >= 95 % of caption-region frames differ from the raw (uncaptioned)
           assembly - captions confirmed burned in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shipcast.clients import ffmpeg_client as ff
from shipcast.manifest import Manifest, StageStatus, dump_json_canonical
from shipcast.project import Project
from shipcast.stages.s08_video import VideoStage

# --------------------------------------------------------------------------- #
# Synthetic-input helpers (real ffmpeg, kept tiny/fast)
# --------------------------------------------------------------------------- #

_CLIP_DUR = 4.5  # 4 clips x 4.5 s = 18 s assembled -> inside 15-25 s.
_NARRATION_DUR = 18.0


def _make_testsrc_clip(path: Path, *, seconds: float, hue: int) -> None:
    """Write a tiny 1080x1920 h264 clip via ffmpeg testsrc (ultrafast)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"testsrc=size=1080x1920:rate=30:duration={seconds},hue=h={hue}",
        "-t", f"{seconds:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-pix_fmt", "yuv420p", "-an",
        "-f", "mp4", str(path),
    ]
    subprocess.run(argv, check=True, capture_output=True)


def _make_sine_mp3(path: Path, *, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
        "-c:a", "libmp3lame", "-b:a", "96k",
        str(path),
    ]
    subprocess.run(argv, check=True, capture_output=True)


def _words_json() -> str:
    """A canned words.json spanning the narration with several caption chunks."""
    words = []
    t = 0.0
    vocab = ["Ship", "faster", "today", "with", "shipcast", "now", "and", "win"]
    while t < _NARRATION_DUR - 0.5:
        w = vocab[len(words) % len(vocab)]
        words.append({"word": w, "start_sec": round(t, 3), "end_sec": round(t + 0.4, 3)})
        t += 0.5
    return dump_json_canonical(words)


def _build_project(tmp_path: Path, *, voice_md: str, with_bgm: bool) -> Project:
    """Materialize a project with 06 + 07 done+approved and all inputs on disk."""
    root = tmp_path / "projects"
    root.mkdir()
    proj = Project.create(
        root,
        "entry",
        {},
        settings=__import__("shipcast.config", fromlist=["Settings"]).Settings(),
        template_path=__import__(
            "shipcast.paths", fromlist=["default_template_path"]
        ).default_template_path(),
    )

    # input.yaml with brand_slug so the stage can locate _brand/<slug>/.
    proj.input_path.write_text(
        "repo_path: /tmp\nentry_heading: X\nbrand_slug: test-brand\n", encoding="utf-8"
    )

    # 06_video_assets clips + clips.json
    va_dir = proj.stage_dir("06_video_assets")
    for i in range(4):
        _make_testsrc_clip(va_dir / f"beat_{i:02d}.mp4", seconds=_CLIP_DUR, hue=i * 60)
    clips = {
        "mode": "standard",
        "clips": [
            {"index": i, "filename": f"beat_{i:02d}.mp4", "source": "ken_burns",
             "duration_sec": _CLIP_DUR}
            for i in range(4)
        ],
    }
    (va_dir / "clips.json").write_text(dump_json_canonical(clips), encoding="utf-8")

    # 07_voice narration + words
    voice_dir = proj.stage_dir("07_voice")
    _make_sine_mp3(voice_dir / "narration.mp3", seconds=_NARRATION_DUR)
    (voice_dir / "words.json").write_text(_words_json(), encoding="utf-8")

    # 03_brand voice.md + proposal.json
    brand_dir = proj.stage_dir("03_brand")
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "voice.md").write_text(voice_md, encoding="utf-8")
    (brand_dir / "proposal.json").write_text(
        dump_json_canonical(
            {"palette": ["#FF6B6B", "#1D2A41", "#F4F1DE"],
             "font_family": "Inter", "logo_detected": True}
        ),
        encoding="utf-8",
    )

    # Optional BGM under _brand/test-brand/music/
    if with_bgm:
        music = root / "_brand" / "test-brand" / "music"
        music.mkdir(parents=True, exist_ok=True)
        _make_sine_mp3(music / "track.mp3", seconds=_NARRATION_DUR)

    # Mark 06 + 07 done + approved in the manifest.
    m = Manifest.load(proj.manifest_path)
    for sid, outs in (
        ("06_video_assets", ("06_video_assets/beat_00.mp4", "06_video_assets/clips.json")),
        ("07_voice", ("07_voice/narration.mp3", "07_voice/words.json")),
    ):
        m = m.transition(sid, StageStatus.RUNNING)
        m = m.transition(sid, StageStatus.DONE, outputs=outs)
        m = m.approve(sid)
    m.save(proj.manifest_path)
    return Project.load(root, "entry")


# --------------------------------------------------------------------------- #
# TC-11.1 - happy path output set + dims/codec/duration
# --------------------------------------------------------------------------- #


def test_tc_11_1_happy_path_outputs(tmp_path: Path) -> None:
    proj = _build_project(tmp_path, voice_md="caption_mode: chip\n", with_bgm=False)
    stage = VideoStage()
    result = stage.run(proj)
    assert result.status == StageStatus.DONE

    out = proj.stage_dir("08_video")
    showcase = out / "showcase.mp4"
    loop_mp4 = out / "loop_6s.mp4"
    loop_gif = out / "loop_6s.gif"
    assert showcase.is_file() and loop_mp4.is_file() and loop_gif.is_file()

    # showcase: 1080x1920, h264 + aac, 15-25 s.
    probe = ff.probe_video(showcase)
    assert (probe.width, probe.height) == (1080, 1920)
    assert probe.codec_name == "h264"
    assert ff.probe_audio_codec(showcase) == "aac"
    assert probe.duration_sec is not None
    assert 15.0 <= probe.duration_sec <= 25.0

    # loop_6s.mp4: 1080x1080, no audio, 6.0 ± 0.1 s.
    lprobe = ff.probe_video(loop_mp4)
    assert (lprobe.width, lprobe.height) == (1080, 1080)
    assert ff.probe_audio_codec(loop_mp4) is None
    assert lprobe.duration_sec is not None
    assert abs(lprobe.duration_sec - 6.0) <= 0.1

    # loop_6s.gif <= 8 MB.
    assert loop_gif.stat().st_size <= 8 * 1024 * 1024

    # outputs are declared relative paths.
    rels = {str(p) for p in result.outputs}
    assert rels == {
        "08_video/showcase.mp4",
        "08_video/loop_6s.mp4",
        "08_video/loop_6s.gif",
    }


# --------------------------------------------------------------------------- #
# TC-11.9 - captions burned in (>= 95 % of caption-region frames differ)
# --------------------------------------------------------------------------- #


def _extract_frames(video: Path, out_dir: Path, *, fps: int = 2) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-vf", f"fps={fps}", "-f", "image2", str(out_dir / "f_%04d.png")],
        check=True, capture_output=True,
    )
    return sorted(out_dir.glob("f_*.png"))


def test_tc_11_9_captions_burned_in(tmp_path: Path) -> None:
    from PIL import Image, ImageChops

    proj = _build_project(tmp_path, voice_md="caption_mode: chip\n", with_bgm=False)
    stage = VideoStage()

    # Raw (uncaptioned) assembly for comparison.
    raw = proj.stage_dir("08_video") / "_raw_for_diff.mp4"
    stage._assemble_raw(proj, raw)

    stage.run(proj)
    showcase = proj.stage_dir("08_video") / "showcase.mp4"

    raw_frames = _extract_frames(raw, tmp_path / "raw_frames")
    cap_frames = _extract_frames(showcase, tmp_path / "cap_frames")
    n = min(len(raw_frames), len(cap_frames))
    assert n >= 5

    # Caption region = bottom ~25 % of the 1080x1920 frame.
    differing = 0
    for rf, cf in zip(raw_frames[:n], cap_frames[:n], strict=False):
        a = Image.open(rf).convert("RGB")
        b = Image.open(cf).convert("RGB")
        if a.size != b.size:
            b = b.resize(a.size)
        w, h = a.size
        box = (0, int(h * 0.70), w, h)
        diff = ImageChops.difference(a.crop(box), b.crop(box))
        if diff.getbbox() is not None:
            differing += 1
    assert differing / n >= 0.95
