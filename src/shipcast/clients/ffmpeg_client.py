"""ffmpeg seam — used by the dispatcher's stage-10 pre-flight and by
Stage 10's `AssembleVideoStage.run()` for video assembly.

``check_available_or_raise`` is the dispatcher's pre-flight check; the
dispatcher calls it BEFORE acquiring the project lock when a dispatched
stage has ``requires_ffmpeg=True``.

``assemble`` is the module-level entry point Stage 10 calls (architect
Ruling 2: NOT a class — mirrors ``check_available_or_raise`` and
``_ffmpeg_path``; the project's lazy-client invariant targets SDK
imports + SecretStr validation, neither of which applies to a stdlib
``subprocess.run`` call against the ffmpeg binary).

``build_concat_file`` returns the textual contents of the concat-demuxer
list file Stage 10 writes atomically before invoking ``assemble``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from shipcast.errors import FfmpegAssembleFailed, FfmpegNotFound

#: Test seam — tests monkeypatch this to simulate ffmpeg presence/absence
#: without touching subprocess. Production code goes through `subprocess.run`.
_WHICH: object = shutil.which

#: Maximum captured stderr length retained on FfmpegAssembleFailed
#: (architect Ruling 8). 2000 chars retains the final error line plus
#: 1-2 preceding warning lines without bloating manifest.json.
_STDERR_TAIL_CHARS: int = 2000


# ── Pre-flight (existing) ────────────────────────────────────────────────


def check_available_or_raise() -> str:
    """Return the first line of `ffmpeg -version`, or raise FfmpegNotFound.

    The dispatcher calls this BEFORE acquiring the project lock when a
    dispatched stage has `requires_ffmpeg=True`. The check is cheap and
    independent of any project state.
    """
    binary = _WHICH("ffmpeg")  # type: ignore[operator]  # _WHICH is callable
    if binary is None:
        raise FfmpegNotFound(
            "ffmpeg not on PATH. "
            "Install via `brew install ffmpeg` (macOS) or your distro's package manager."
        )
    try:
        result = subprocess.run(
            [str(binary), "-version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise FfmpegNotFound(f"ffmpeg invocation failed: {exc}") from exc
    return result.stdout.splitlines()[0] if result.stdout else "ffmpeg (version unknown)"


def _ffmpeg_path() -> Path | None:
    """Internal helper exposed for tests that want the resolved binary path."""
    binary = shutil.which("ffmpeg")
    return Path(binary) if binary else None


# ── concat demuxer list-file builder ─────────────────────────────────────


#: Target video framerate (must match the ``-r`` flag in ``_build_argv``).
#: Exposed as a module constant so ``frame_align_durations`` and the argv
#: stay in lockstep. Bumping fps trims per-cut quantization headroom
#: linearly (33ms at 30 → 17ms at 60); the post-encode duration delta in
#: Stage 10 drops correspondingly.
_VIDEO_FPS: int = 30


def frame_align_durations(durations_sec: list[float], fps: int = _VIDEO_FPS) -> list[float]:
    """Snap each duration to a frame boundary while preserving the total.

    ffmpeg's concat demuxer at ``-r <fps>`` quantizes each image's display
    duration to the nearest ``1/fps`` (≈33 ms at 30 fps). With 76 cuts of
    arbitrary word-boundary timestamps the per-cut rounding errors
    compound and the assembled MP4 ends up 0.5-1s shorter than the
    narration — visible in playback as images appearing AHEAD of the
    spoken word they illustrate.

    Pre-snapping each duration to a frame boundary eliminates rounding at
    the demuxer layer. The cumulative-error compensation (target vs
    running snapped) keeps the total within half a frame of the input
    total, so per-cut drift never accumulates: the worst-case offset at
    any cut boundary is ``0.5/fps`` regardless of cut count.

    Args:
        durations_sec: raw per-cut durations (typically ``cut.end_sec -
            cut.start_sec`` from Stage 06's cuts.json).
        fps: target framerate. Defaults to ``_VIDEO_FPS`` (must match the
            ``-r`` argv flag in ``_build_argv``).

    Returns:
        Snapped durations, same length as the input.

    Raises:
        ValueError: if ``fps`` is not positive.
    """
    if fps <= 0:
        raise ValueError(f"fps must be positive; got {fps}")
    frame_duration = 1.0 / fps
    cum_target = 0.0
    cum_snapped = 0.0
    snapped: list[float] = []
    for d in durations_sec:
        cum_target += d
        # How many frames to advance ``cum_snapped`` to land closest to
        # ``cum_target``?
        needed_frames = round((cum_target - cum_snapped) / frame_duration)
        # The concat demuxer rejects zero-duration entries; hold for at
        # least one frame even if rounding would have produced 0.
        needed_frames = max(needed_frames, 1)
        d_snapped = needed_frames * frame_duration
        snapped.append(d_snapped)
        cum_snapped += d_snapped
    return snapped


def build_concat_file(image_paths: list[Path], durations_sec: list[float]) -> str:
    """Build the textual content of an ffmpeg concat-demuxer list file.

    Architect Ruling 5: paths are absolute (the caller supplies them in
    absolute form). Each ``file`` directive uses single quotes so any
    spaces or special characters in the absolute path survive the demuxer
    parse.

    HISTORICAL NOTE: the original Stage 10 design omitted the trailing
    ``duration`` line on the last entry, assuming ffmpeg would hold the
    final image until the audio track ended and ``-shortest`` would clip
    the output. That assumption proved wrong on ffmpeg 8.x with image
    inputs — the last image is held for ~1 frame (or some demuxer-
    chosen default that empirically came in around 3.5s on atomic-
    habits), producing a video shorter than the audio. With Stage 06's
    contract that ``cuts[-1].end_sec == audio_duration_sec``, the sum
    of ALL durations already equals the audio length, so emitting a
    duration on the last entry too gives an exact match without any
    ``-shortest`` cleverness. Stage 10's pre-encode
    ``frame_align_durations`` keeps that sum within half a frame of
    the audio length.

    Args:
        image_paths: ordered list of absolute paths to scene JPEGs.
        durations_sec: ordered list of per-image durations in seconds.
            Must have the same length as ``image_paths``.

    Returns:
        A string suitable for writing to ``10_video/concat.txt`` via
        the stage's atomic temp+replace pattern. The string ends with
        a single trailing newline.

    Raises:
        ValueError: if the lengths differ or either list is empty.
    """
    if not image_paths:
        raise ValueError("build_concat_file requires at least one image")
    if len(image_paths) != len(durations_sec):
        raise ValueError(
            f"build_concat_file: image_paths and durations_sec length mismatch — "
            f"{len(image_paths)} paths vs {len(durations_sec)} durations"
        )
    lines: list[str] = []
    for path, duration in zip(image_paths, durations_sec, strict=True):
        lines.append(f"file '{path}'")
        lines.append(f"duration {duration:.6f}")
    return "\n".join(lines) + "\n"


# ── Output dataclass for the assemble call ───────────────────────────────


@dataclass(frozen=True)
class AssembleResult:
    """Successful return value from `assemble`.

    Failed runs raise ``FfmpegAssembleFailed`` instead of returning.
    """

    returncode: int
    stdout: str
    stderr: str
    wall_clock_sec: float


# ── The main entry point ─────────────────────────────────────────────────


def assemble(
    *,
    concat_path: Path,
    audio_path: Path,
    output_path: Path,
    audio_duration_sec: float,
) -> AssembleResult:
    """Run ffmpeg to encode the final MP4 from a concat list + audio.

    ONE subprocess call per invocation. Architect Ruling 2 (module-level
    function) + Ruling 4 (atomic temp+replace — the CALLER is expected to
    pass ``output_path`` pointing at the ``.tmp`` location; the swap to
    the final path is the caller's responsibility, mirroring stages 03/09).

    The exact ffmpeg command (architect-locked per PRD FR-10.16):

    .. code-block:: text

        ffmpeg -y \\
          -f concat -safe 0 -i {concat_path} \\
          -i {audio_path} \\
          -vf "scale=1920:1080:force_original_aspect_ratio=decrease,
               pad=1920:1080:(ow-iw)/2:(oh-ih)/2" \\
          -r 30 -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \\
          -c:a aac -b:a 128k \\
          -shortest \\
          -movflags +faststart \\
          {output_path}

    Args:
        concat_path: absolute path to the concat demuxer list file
            (written via temp+replace by the caller before this call).
        audio_path: absolute path to ``03_audio/narration.mp3``.
        output_path: absolute path to where ffmpeg should write its
            output. Architect Ruling 4 expects the caller to pass a
            ``.tmp`` path; the caller does the ``os.replace`` to the
            final path on success.

    Returns:
        AssembleResult with returncode, stdout, stderr, wall_clock_sec
        on exit-code 0.

    Raises:
        FfmpegAssembleFailed: ffmpeg exited non-zero. The error carries
            ``returncode`` and ``stderr_tail`` (last 2000 chars) per
            architect Ruling 8.
    """
    argv = _build_argv(
        concat_path=concat_path,
        audio_path=audio_path,
        output_path=output_path,
        audio_duration_sec=audio_duration_sec,
    )
    before = time.monotonic()
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
    )
    wall_clock_sec = round(time.monotonic() - before, 3)
    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-_STDERR_TAIL_CHARS:]
        raise FfmpegAssembleFailed(
            returncode=result.returncode, stderr_tail=stderr_tail
        )
    return AssembleResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        wall_clock_sec=wall_clock_sec,
    )


# ── Ken-Burns clip (Stage 06) ────────────────────────────────────────────


#: Vertical showcase frame — every Stage-06 clip is exactly this size.
VERTICAL_WIDTH: int = 1080
VERTICAL_HEIGHT: int = 1920


@dataclass(frozen=True)
class ProbeResult:
    """ffprobe view of a video's first stream + container duration."""

    codec_name: str | None
    width: int | None
    height: int | None
    duration_sec: float | None


def ken_burns_clip(
    *,
    still_path: Path,
    duration_sec: float,
    output_path: Path,
    fast: bool = False,
) -> Path:
    """Render one still PNG into a slow pan/zoom 1080x1920 h264 MP4.

    The filter pre-scales the still WAY up (``scale=8000:-1``) so ``zoompan`` has
    sub-pixel headroom for a smooth slow push-in without the characteristic
    1-px-per-frame jitter, then ``zoompan`` eases the zoom from 1.0 to 1.20 over
    the clip and ``scale``/``crop`` settle the frame at the exact 1080x1920
    showcase size. ONE subprocess call per invocation.

    Args:
        still_path: absolute path to the source PNG.
        duration_sec: clip length in seconds (Stage 06 passes 3-5 s).
        output_path: absolute path the MP4 is written to.
        fast: when True, use ``-preset ultrafast -crf 28`` (test renders);
            otherwise the production ``-preset medium -crf 23``.

    Returns:
        ``output_path`` on success.

    Raises:
        FfmpegAssembleFailed: ffmpeg exited non-zero (e.g. a missing still).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(duration_sec * _VIDEO_FPS))
    preset = "ultrafast" if fast else "medium"
    crf = "28" if fast else "23"
    vf = (
        "scale=8000:-1,"
        f"zoompan=z='min(zoom+0.0010,1.20)':"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={VERTICAL_WIDTH}x{VERTICAL_HEIGHT}:fps={_VIDEO_FPS},"
        "format=yuv420p"
    )
    argv = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-loop",
        "1",
        "-i",
        str(still_path),
        "-t",
        f"{duration_sec:.3f}",
        "-vf",
        vf,
        "-r",
        str(_VIDEO_FPS),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
        "-an",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-_STDERR_TAIL_CHARS:]
        raise FfmpegAssembleFailed(returncode=result.returncode, stderr_tail=stderr_tail)
    return output_path


def probe_video(path: Path) -> ProbeResult:
    """Return the first video stream's codec/dimensions + container duration.

    Uses one ``ffprobe`` call. Missing fields come back as ``None`` so the caller
    (Stage 06's clip validator) can raise a structured ``ClipValidationFailed``
    rather than crashing on a malformed probe.
    """
    argv = [
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
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()

    def _as_int(value: str | None) -> int | None:
        if value in (None, "", "N/A"):
            return None
        try:
            return int(value)  # type: ignore[arg-type]  # narrowed above
        except ValueError:
            return None

    def _as_float(value: str | None) -> float | None:
        if value in (None, "", "N/A"):
            return None
        try:
            return float(value)  # type: ignore[arg-type]  # narrowed above
        except ValueError:
            return None

    codec = fields.get("codec_name")
    return ProbeResult(
        codec_name=codec if codec not in (None, "", "N/A") else None,
        width=_as_int(fields.get("width")),
        height=_as_int(fields.get("height")),
        duration_sec=_as_float(fields.get("duration")),
    )


def _build_argv(
    *,
    concat_path: Path,
    audio_path: Path,
    output_path: Path,
    audio_duration_sec: float,
) -> list[str]:
    """Return the exact ffmpeg argv per FR-10.16. Pure function for testability."""
    return [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_path),
        "-i", str(audio_path),
        "-vf",
        # Three-step video filter chain:
        # 1. scale  — fit each scene into 1920x1080 preserving aspect
        # 2. pad    — letterbox/pillarbox to exactly 1920x1080
        # 3. tpad   — INTERNAL safety pad: extend the video by 2 s past
        #             concat's reported end so libx264's B-frame lookahead
        #             (which silently drops ~0.7 s of frames at concat's
        #             tail) has room to flush. WITHOUT this the trailing
        #             ~0.7 s of audio would be clipped by `-shortest`
        #             (the original atomic-habits "vote" mid-syllable
        #             cut). The tpad tail itself is never visible in the
        #             output: the explicit `-t {audio_duration_sec}` at
        #             the end of this argv caps the muxed output at the
        #             narration's real end, so the black tpad pad AND
        #             any concat frame-rounding overage are both trimmed
        #             before the file is written.
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
        "tpad=stop_duration=2",
        "-r", str(_VIDEO_FPS),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        # Pad the source narration with 5 s of trailing silence BEFORE
        # the AAC encode. Without it, the AAC frame-boundary trimming at
        # the codec layer chops ~0.5-0.7 s off the end of the source
        # audio (mp3 LAME-padding + AAC frame alignment + encoder priming
        # all interact badly). The eventual `-t` caps the output at the
        # source narration's exact end, so the apad silence buffer is
        # also trimmed off — the user never hears it.
        "-af", "apad=pad_dur=5",
        "-shortest",
        # Cap the muxed output at the narration's exact duration. tpad
        # and apad above are internal safety pads to defeat encoder-tail
        # truncation; -t trims both off so the final file ends precisely
        # at the last spoken syllable. No black-frame tail, no concat
        # frame-rounding overage in the published asset.
        "-t", f"{audio_duration_sec:.3f}",
        "-movflags", "+faststart",
        # Stage 10 writes atomically to a `.tmp` path and renames on success.
        # ffmpeg 5.x could infer the muxer from the parent stem (.mp4.tmp →
        # mp4); ffmpeg 8.x cannot and exits with "Unable to choose an output
        # format". Naming the muxer explicitly keeps the contract stable
        # across ffmpeg versions.
        "-f", "mp4",
        str(output_path),
    ]
