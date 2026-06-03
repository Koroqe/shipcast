"""Audio measurement utilities shared across pipeline stages.

`measure_audio_duration_sec` is the single source of truth for ffprobe-
based duration measurement. Stage 03 uses it to record the just-synthesized
narration's length; Stage 04 uses it to re-measure the same file as the
duration tolerance gate. The body was originally a private helper inside
`shipcast.stages.s03_generate_voice` (`_measure_audio_duration_sec`); it was
lifted here in Stage 04 Slice 1 so both stages share the same code path
without one importing private names from the other.

The function is intentionally a pure stateless helper, not a class. There
is no SDK state to carry, no SecretStr handling, and no per-stage policy —
just a subprocess call to `ffprobe` and a float parse. That keeps it out
of `shipcast.clients/`, which is reserved for stateful API client wrappers.

Exception mapping (preserved verbatim from the prior Stage 03 helper):

- `FileNotFoundError` (ffprobe binary missing on `PATH`) → `FfmpegNotFound`
  with the `"brew install ffmpeg"` remediation hint.
- Non-zero return code, empty stdout, or non-float stdout → `RuntimeError`
  with the literal prefix `"ffprobe failed: "` plus the trimmed stderr.

`subprocess.run(..., timeout=30)` is preserved as well — a hung ffprobe
must not deadlock a stage run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shipcast.errors import FfmpegNotFound


def measure_audio_duration_sec(mp3_path: Path) -> float:
    """Return the audio duration of `mp3_path` (in seconds) via `ffprobe`.

    Raises:
        FfmpegNotFound: if the `ffprobe` binary is not on `PATH`.
        RuntimeError: if ffprobe returns non-zero, emits empty stdout, or
            emits stdout that does not parse as a float. The message is
            always `"ffprobe failed: <trimmed stderr>"` so callers can grep.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(mp3_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FfmpegNotFound(
            "ffprobe not found — install ffmpeg "
            "(e.g. brew install ffmpeg) and ensure it is on PATH"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    stdout_stripped = result.stdout.strip()
    if not stdout_stripped:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    try:
        return float(stdout_stripped)
    except ValueError as exc:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}") from exc
