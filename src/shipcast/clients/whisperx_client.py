"""WhisperX-shaped client backed by openai-whisper — Stage 05 (`word_timestamps`).

IMPLEMENTATION NOTE — name vs. underlying library:
The class is still called ``WhisperXClient`` and the config field is still
``whisperx_model``, but the underlying library is now ``openai-whisper``
(the original Python implementation), NOT WhisperX. The original Stage 05
design targeted WhisperX for its forced-alignment word timestamps
(~10-50 ms accuracy), but WhisperX 3.x strictly requires ``ctranslate2 >= 4.5``
and ``ctranslate2 >= 4`` dropped Intel-Mac wheels — leaving Intel-Mac
operators with an unbuildable dependency stack. ``openai-whisper`` has the
same word_timestamps output shape, ~50-150 ms alignment accuracy (looser
than WhisperX but still acceptable for cut-point scheduling downstream),
and a torch-only dependency that works across all supported platforms.

The class and config-field names are preserved to avoid mass-renames across
4+ stage files, 10+ test files, and security.md — the rename would be pure
churn since the public interface (``transcribe_with_alignment`` returning
``list[WordTimestamp]``) is identical.

Local model — NO API key, NO secret-handling, NO network egress to
operator-paid endpoints. The constructor takes zero arguments; the
underlying ``openai-whisper`` SDK is OPTIONAL (declared under
``[project.optional-dependencies] whisperx``) and is imported lazily INSIDE
``transcribe_with_alignment`` so that the rest of the CLI works for
operators who haven't run ``uv sync --extra whisperx``.

The transcription is a two-step sequence:

1. ``whisper.load_model(model_name, device=...)`` — downloads the
   ~150 MB ``base.en`` weights on first run, then caches them in
   ``~/.cache/whisper/``.
2. ``model.transcribe(audio_path, word_timestamps=True, fp16=False)`` —
   chunk-level transcription + per-word timestamps in a single call (no
   separate alignment model needed, unlike WhisperX).

Word-segment mapping (FR-5.8) is preserved:
- ``word`` = ``entry["word"].strip()`` — openai-whisper inserts leading
  whitespace on most word tokens, so the strip is essential. Words that
  strip to empty are dropped before construction (the schema's
  ``min_length=1`` would reject them otherwise).
- ``start_sec`` / ``end_sec`` = ``float(entry["start" | "end"])``.
- ``confidence`` = ``entry.get("probability")`` then ``None`` if the value
  is ``None`` or NaN, else clamped to ``[0.0, 1.0]``. (WhisperX called
  this ``score``; openai-whisper calls it ``probability``.)

Hard floor (FR-5.9): a zero-word result raises
``ValueError("WhisperX returned 0 words — check that the audio contains
speech")`` — the em-dash is U+2014. No empty JSON is ever written. The
error message keeps the literal string "WhisperX" because that is the
contract Stage 05 surfaces to the operator and downstream tests pin to it.

``fp16=False`` is passed because the operator's hardware budget is CPU
(torch's CPU backend does not support fp16). ``verbose=False`` suppresses
openai-whisper's progress prints — Stage 05 surfaces its own logging.

Greedy decoding makes the underlying model deterministic for fixed input +
model weights, so ``--rerun`` produces byte-identical ``words.json``
(NFR-5.7) — same as WhisperX.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Slice 1 quarantine: ``shipcast.schemas`` lands in Slice 3. The annotation
    # below is only needed for type-checking; the runtime construction lazy-
    # imports ``WordTimestamp`` inside ``transcribe_with_alignment`` so this
    # module import-parses without ``shipcast.schemas`` existing yet.
    from shipcast.schemas import WordTimestamp  # type: ignore[import-untyped]


class WhisperXClient:
    """Local speech-to-text client. Construct inside ``stage.run()`` only.

    Despite the name, the underlying library is ``openai-whisper`` — see
    the module docstring for the rationale.
    """

    def __init__(self) -> None:
        # No state to initialize. No API key, no torch handle, no SDK
        # import. The constructor exists so the test/integration patches
        # have a stable shape to spy on; it is intentionally trivial.
        return

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    def transcribe_with_alignment(
        self,
        mp3_path: Path,
        model_name: str,
        *,
        device: str = "cpu",
    ) -> list[WordTimestamp]:
        """Transcribe ``mp3_path`` and return per-word timestamps.

        See module docstring for the two-step sequence and the
        word-segment mapping rules.

        Args:
            mp3_path: absolute path to the narration MP3.
            model_name: openai-whisper model id (e.g. ``"base.en"``,
                ``"small.en"``). Same names WhisperX accepts.
            device: hardware device. v1 always passes ``"cpu"``; the
                keyword-only argument exists as a forward seam for
                future GPU/MPS support.

        Returns:
            A list of ``WordTimestamp`` objects, non-empty by construction
            (a zero-word return raises ``ValueError``).

        Raises:
            ModuleNotFoundError: if the ``whisperx`` extra is not
                installed. Operator remediation: ``uv sync --extra whisperx``.
            ValueError: if transcription returns zero words.
            Any underlying SDK exception: propagates unchanged (no
                custom wrapping). Includes model-download failures
                (``OSError``, ``ConnectionError``) on first run and torch
                runtime errors.
        """
        # LAZY IMPORT: lives ONLY here so the rest of the CLI works for
        # operators without ``uv sync --extra whisperx``. NFR-5.2 / NFR-5.3
        # asserted by subprocess test. mypy can't find the stub because
        # the dependency is optional - ignore is correct, not a smell.
        import whisper  # type: ignore[import-not-found]

        from shipcast.schemas import WordTimestamp  # Slice 3 dependency (lazy)

        # Step 1: load the transcription model (downloads on first run).
        model = whisper.load_model(model_name, device=device)

        # Step 2: transcribe + extract word-level timestamps in one call.
        # fp16=False is required on CPU (torch CPU backend lacks fp16).
        # verbose=False suppresses progress prints to stderr.
        transcribe_result = model.transcribe(
            str(mp3_path),
            word_timestamps=True,
            fp16=False,
            verbose=False,
        )

        # Map segments[*].words -> list[WordTimestamp]. Skip stripped-empty
        # words; clamp confidence; treat NaN / None as None.
        word_timestamps: list[WordTimestamp] = []
        for segment in transcribe_result.get("segments", []):
            for entry in segment.get("words", []):
                word_text = str(entry["word"]).strip()
                if not word_text:
                    continue
                probability = entry.get("probability")
                if probability is None:
                    confidence: float | None = None
                else:
                    try:
                        prob_f = float(probability)
                    except (TypeError, ValueError):
                        confidence = None
                    else:
                        if math.isnan(prob_f):
                            confidence = None
                        else:
                            confidence = min(1.0, max(0.0, prob_f))
                word_timestamps.append(
                    WordTimestamp(
                        word=word_text,
                        start_sec=float(entry["start"]),
                        end_sec=float(entry["end"]),
                        confidence=confidence,
                    )
                )

        if not word_timestamps:
            raise ValueError(
                "WhisperX returned 0 words — check that the audio contains speech"
            )

        return word_timestamps
