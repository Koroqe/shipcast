"""Stage 08 - video assembly (concat + audio mix + captions + 6 s loop).

Turns the approved Stage-06 clips (``06_video_assets/beat_{00..03}.mp4``) and
Stage-07 narration (``07_voice/narration.mp3`` + ``07_voice/words.json``) into
three artifacts under ``08_video/``:

* ``showcase.mp4`` - the full vertical reel: the four clips concatenated, with
  narration as the primary audio (and, if the brand pack ships background music,
  the bed ducked -3 dB under the narration), then caption frames burned in.
  1080x1920, h264 + aac, 15-25 s.
* ``loop_6s.mp4``  - the first 6 s of the hero clip (beat[0]), center-cropped to
  a 1080x1080 square, audio stripped. 6.0 s ± 0.1.
* ``loop_6s.gif``  - a small GIF export of the square loop (<= 8 MB).

Caption mode (FR-10.2 / FR-14.8 / Architect MAJOR Finding 1)
-----------------------------------------------------------
The caption renderer mode is read from the ``caption_mode:`` line in the
CANONICAL ``03_brand/voice.md`` (the copy Stage-03 made, NOT the raw
``_brand/<slug>/`` pack). Recognized values are ``chip`` (default), ``karaoke``,
``reveal``; an absent line or an unrecognized value falls back to ``chip``
WITHOUT raising.

Background music (FR-10.1)
--------------------------
If ``_brand/<slug>/music/`` contains any ``.mp3`` / ``.wav``, the
first-alphabetical track is mixed under the narration with the bed ducked
-3 dB. With no music, narration is the sole audio track and no duck filter is
applied.

Purity
------
PIL + the ``composition`` helpers are imported LAZILY inside the methods that
need them (never at module top) so ``import shipcast.cli`` - which imports
``shipcast.stages`` - does not pull PIL into ``sys.modules``. ffmpeg is shelled
out via :mod:`shipcast.clients.ffmpeg_client` and pre-flighted by the dispatcher
(``requires_ffmpeg=True``). This stage calls no paid API, so its cost is $0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from shipcast.clients import ffmpeg_client as _ffmpeg
from shipcast.errors import StageInputMissing, StageOutputInvalid
from shipcast.manifest import StageStatus
from shipcast.schemas import BrandProposal, VideoBeats
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from shipcast.composition.captions import CaptionMode, Palette, WordDict
    from shipcast.project import Project

#: Canonical brand artifacts (Finding-1 copy lives under 03_brand/).
_VOICE_MD_REL: str = "03_brand/voice.md"
_PROPOSAL_REL: str = "03_brand/proposal.json"
_CLIPS_REL: str = "06_video_assets/clips.json"
_WORDS_REL: str = "07_voice/words.json"
_NARRATION_REL: str = "07_voice/narration.mp3"

#: Brand-pack music directory (operator-placed, outside the upstream outputs).
_MUSIC_DIRNAME: str = "music"
_MUSIC_SUFFIXES: tuple[str, ...] = (".mp3", ".wav")

#: Fallback palette if the brand proposal has fewer than 3 hex codes.
_FALLBACK_PALETTE: tuple[str, str, str] = ("#1D2A41", "#FF6B6B", "#F4F1DE")


class VideoStage(BaseStage):
    """Assemble ``08_video/{showcase.mp4, loop_6s.mp4, loop_6s.gif}``."""

    id: ClassVar[str] = "08_video"
    requires: ClassVar[tuple[str, ...]] = ("06_video_assets", "07_voice")
    output_schema: ClassVar[type[VideoBeats]] = VideoBeats  # multi-output; unused
    requires_ffmpeg: ClassVar[bool] = True
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Watch showcase.mp4 end-to-end - confirm the cuts, narration sync, and "
        "captions read cleanly and stay on-brand.",
        "Confirm captions are legible against every clip's background and the "
        "current word is highlighted in step with the voiceover.",
        "Check loop_6s.mp4 / loop_6s.gif - the square crop frames the hero shot "
        "well and the loop is seamless.",
    )

    SHOWCASE_FILENAME: ClassVar[str] = "showcase.mp4"
    LOOP_MP4_FILENAME: ClassVar[str] = "loop_6s.mp4"
    LOOP_GIF_FILENAME: ClassVar[str] = "loop_6s.gif"

    # ------------------------------------------------------------- helpers

    def _resolve_caption_mode(self, project: Project) -> CaptionMode:
        """Return the caption mode from ``03_brand/voice.md`` (default ``chip``).

        Reads the canonical Finding-1 copy. A missing file OR an absent/
        unrecognized ``caption_mode:`` line resolves to ``chip`` without raising.
        """
        from shipcast.composition.captions import parse_caption_mode

        path = project.path / _VOICE_MD_REL
        if not path.is_file():
            return "chip"
        return parse_caption_mode(path.read_text(encoding="utf-8"))

    def _resolve_bgm(self, project: Project) -> Path | None:
        """Return the first-alphabetical ``_brand/<slug>/music/*`` track, or None.

        ``brand_slug`` is read from ``input.yaml`` without full validation (the
        SSRF/path defenses are not relevant to locating the music dir).
        """
        brand_slug = self._read_brand_slug(project)
        if brand_slug is None:
            return None
        music_dir = project.root / "_brand" / brand_slug / _MUSIC_DIRNAME
        if not music_dir.is_dir():
            return None
        tracks = sorted(
            p
            for p in music_dir.iterdir()
            if p.is_file() and p.suffix.lower() in _MUSIC_SUFFIXES
        )
        return tracks[0] if tracks else None

    @staticmethod
    def _read_brand_slug(project: Project) -> str | None:
        """Read ``brand_slug`` from ``input.yaml`` (None if missing/malformed)."""
        import yaml

        path = project.input_path
        if not path.is_file():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return None
        if isinstance(data, dict):
            value = data.get("brand_slug")
            if isinstance(value, str) and value:
                return value
        return None

    def _resolve_palette(self, project: Project) -> Palette:
        """Build the caption palette from ``03_brand/proposal.json`` (>=3 hex)."""
        from shipcast.composition.captions import brand_palette

        path = project.path / _PROPOSAL_REL
        primary, accent, neutral = _FALLBACK_PALETTE
        if path.is_file():
            proposal = BrandProposal.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            hexes = proposal.palette
            if len(hexes) >= 1:
                primary = hexes[0]
            if len(hexes) >= 2:
                accent = hexes[1]
            if len(hexes) >= 3:
                neutral = hexes[2]
        return brand_palette(primary, accent, neutral)

    def _brand_font_path(self, project: Project) -> Path | None:
        """First ``.ttf`` under ``_brand/<slug>/fonts/`` (display font), if any."""
        brand_slug = self._read_brand_slug(project)
        if brand_slug is None:
            return None
        fonts_dir = project.root / "_brand" / brand_slug / "fonts"
        if not fonts_dir.is_dir():
            return None
        fonts = sorted(fonts_dir.glob("*.ttf"))
        return fonts[0] if fonts else None

    def _load_clip_paths(self, project: Project) -> list[Path]:
        """Ordered absolute paths to the Stage-06 clips from ``clips.json``."""
        clips_path = project.path / _CLIPS_REL
        if not clips_path.is_file():
            raise StageInputMissing(
                f"stage {self.id!r} requires {_CLIPS_REL} to exist"
            )
        clips = VideoBeats.model_validate_json(clips_path.read_text(encoding="utf-8"))
        va_dir = project.stage_dir("06_video_assets")
        paths = [va_dir / c.filename for c in clips.clips]
        missing = [p for p in paths if not p.is_file()]
        if missing:
            raise StageInputMissing(
                f"stage {self.id!r}: missing clip files {[p.name for p in missing]}"
            )
        return paths

    # ------------------------------------------------------------- assembly

    def _assemble_raw(self, project: Project, output_path: Path) -> Path:
        """Concat the clips + mix narration (optionally + ducked BGM) -> raw mp4.

        Produces the uncaptioned, audio-complete intermediate the caption pass
        overlays onto. Returns ``output_path``.
        """
        stage_dir = project.stage_dir(self.id)
        stage_dir.mkdir(parents=True, exist_ok=True)

        clip_paths = self._load_clip_paths(project)
        # concat list-file (duration line per clip so the demuxer holds each
        # clip its full length).
        durations = self._probe_durations(clip_paths)
        concat_txt = stage_dir / "_concat.txt"
        concat_txt.write_text(
            _ffmpeg.build_concat_file(clip_paths, durations), encoding="utf-8"
        )

        silent = stage_dir / "_concat.mp4"
        _ffmpeg.concat_clips(concat_path=concat_txt, output_path=silent)

        narration = project.path / _NARRATION_REL
        if not narration.is_file():
            raise StageInputMissing(
                f"stage {self.id!r} requires {_NARRATION_REL} to exist"
            )
        bgm = self._resolve_bgm(project)
        _ffmpeg.mix_audio(
            video_path=silent,
            narration_path=narration,
            output_path=output_path,
            bgm_path=bgm,
        )
        # Tidy intermediates.
        concat_txt.unlink(missing_ok=True)
        silent.unlink(missing_ok=True)
        return output_path

    @staticmethod
    def _probe_durations(clip_paths: list[Path]) -> list[float]:
        """ffprobe each clip's duration (used to build the concat list-file)."""
        out: list[float] = []
        for p in clip_paths:
            probe = _ffmpeg.probe_video(p)
            out.append(probe.duration_sec if probe.duration_sec else 0.001)
        return out

    def _overlay_captions(
        self, project: Project, raw_path: Path, output_path: Path
    ) -> Path:
        """Render caption frames in the resolved mode and burn them onto ``raw``.

        Returns ``output_path`` (the final ``showcase.mp4``).
        """
        from shipcast.composition import captions as _captions

        fps = _ffmpeg.VIDEO_FPS
        mode = self._resolve_caption_mode(project)
        palette = self._resolve_palette(project)
        font_path = self._brand_font_path(project)
        words = self._load_words(project)

        probe = _ffmpeg.probe_video(raw_path)
        duration = probe.duration_sec or 0.0
        total_frames = int(duration * fps) + 1

        stage_dir = project.stage_dir(self.id)
        frames_dir = stage_dir / "_caption_frames"
        _captions.render_caption_frames(
            words,
            total_frames=total_frames,
            fps=fps,
            palette=palette,
            mode=mode,
            out_dir=frames_dir,
            font_path=font_path,
        )
        _ffmpeg.overlay_captions(
            video_path=raw_path,
            frames_glob=str(frames_dir / "f_%05d.png"),
            fps=fps,
            output_path=output_path,
        )
        # Cleanup the frame PNGs (kept off the declared outputs).
        for f in frames_dir.glob("f_*.png"):
            f.unlink(missing_ok=True)
        frames_dir.rmdir()
        return output_path

    def _load_words(self, project: Project) -> list[WordDict]:
        """Read ``07_voice/words.json`` into the caption renderer's word shape.

        ``words.json`` is a non-empty ``list[WordTimestamp]`` (validated by
        Stage 07), so each element already carries ``word``/``start_sec``/
        ``end_sec`` - exactly the :class:`WordDict` shape the caption renderer
        consumes. We only keep those three keys.
        """
        from typing import cast

        path = project.path / _WORDS_REL
        if not path.is_file():
            raise StageInputMissing(
                f"stage {self.id!r} requires {_WORDS_REL} to exist"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise StageInputMissing(
                f"stage {self.id!r}: {_WORDS_REL} must be a JSON list"
            )
        words: list[WordDict] = [
            cast(
                "WordDict",
                {
                    "word": str(item["word"]),
                    "start_sec": float(item["start_sec"]),
                    "end_sec": float(item["end_sec"]),
                },
            )
            for item in data
        ]
        return words

    def _export_loop(self, project: Project, source_clip: Path) -> tuple[Path, Path]:
        """Export the square 6 s loop (mp4 + gif) from the opening of ``source_clip``.

        Sources the loop from the assembled reel's first 6 s (which always spans
        the hero/opening shot) rather than from a single Stage-06 beat: standard
        beats run 3-5 s, shorter than the required 6.0 s loop, so a single beat
        cannot satisfy the duration contract. The assembled reel is >= 15 s, so
        its first 6 s is exactly 6.0 s after the ``-t 6`` cap.
        """
        stage_dir = project.stage_dir(self.id)
        mp4 = stage_dir / self.LOOP_MP4_FILENAME
        gif = stage_dir / self.LOOP_GIF_FILENAME
        _ffmpeg.export_loop(source_path=source_clip, mp4_path=mp4, gif_path=gif)
        return mp4, gif

    # ------------------------------------------------------------- run

    def run(self, project: Project) -> StageResult:
        stage_dir = project.stage_dir(self.id)
        stage_dir.mkdir(parents=True, exist_ok=True)

        # 1) concat + audio mix -> raw intermediate.
        raw = stage_dir / "_raw.mp4"
        self._assemble_raw(project, raw)

        # 2) caption overlay -> showcase.mp4.
        showcase = stage_dir / self.SHOWCASE_FILENAME
        self._overlay_captions(project, raw, showcase)

        # 3) 6 s square loop (mp4 + gif) from the assembled reel's opening 6 s.
        #    Sourced from the (uncaptioned) raw reel so the loop is clean; the
        #    raw reel is >= 15 s, guaranteeing the 6.0 s loop length.
        self._export_loop(project, raw)
        raw.unlink(missing_ok=True)

        outputs = (
            Path(self.id) / self.SHOWCASE_FILENAME,
            Path(self.id) / self.LOOP_MP4_FILENAME,
            Path(self.id) / self.LOOP_GIF_FILENAME,
        )
        return StageResult(
            status=StageStatus.DONE,
            outputs=outputs,
            metrics={
                "cost_usd": 0.0,
                "caption_mode": self._resolve_caption_mode(project),
            },
        )

    # ------------------------------------------------------------- outputs

    def validate_outputs(self, project: Project, result: StageResult) -> None:
        """Path-safety on all three outputs + magic-byte sanity on the videos.

        The stage emits two MP4s + one GIF, so the default single-schema check
        does not apply. We run the shared path-traversal guard and confirm the
        showcase + loop carry an ``ftyp`` MP4 box and the GIF a ``GIF8`` header.
        """
        self._validate_output_paths(project, result)
        showcase = (project.path / Path(self.id) / self.SHOWCASE_FILENAME).resolve()
        loop_mp4 = (project.path / Path(self.id) / self.LOOP_MP4_FILENAME).resolve()
        loop_gif = (project.path / Path(self.id) / self.LOOP_GIF_FILENAME).resolve()
        for mp4 in (showcase, loop_mp4):
            head = mp4.read_bytes()[:32]
            if b"ftyp" not in head:
                raise StageOutputInvalid(
                    f"stage {self.id!r} output {mp4} is not a valid MP4 (no ftyp box)"
                )
        if loop_gif.read_bytes()[:4] != b"GIF8":
            raise StageOutputInvalid(
                f"stage {self.id!r} output {loop_gif} is not a valid GIF"
            )
