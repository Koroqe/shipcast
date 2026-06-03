"""Stage 07 — voice synthesis (ElevenLabs narration + WhisperX word timestamps).

Turns the approved showcase ``Storyboard`` (``05_script/storyboard.json``) into
two artifacts under ``07_voice/``:

* ``narration.mp3`` — the full voiceover, synthesized by ElevenLabs from the
  beat narration lines joined with a single ``\\n`` (no trailing newline).
* ``words.json`` — a non-empty ``list[WordTimestamp]`` (``{word, start_sec,
  end_sec, confidence}``) produced by the local WhisperX-shaped client aligning
  the synthesized MP3.

Voice identity (FR-9.2): the ElevenLabs ``voice_id`` and ``voice_settings`` come
from ``Settings`` ONLY. ``03_brand/voice.md`` constrains the LLM's *tone* in the
copy/script stages; it NEVER overrides ``Settings.voice_id`` here.

Pre-flight (UC-9-E3): ``check_inputs`` verifies the ``openai-whisper`` package
(the optional ``whisperx`` extra, imported as ``whisper``) is importable BEFORE
any synthesis can run, so a missing backend fails fast without spending an
ElevenLabs call.

Error handling
--------------
* ElevenLabs HTTP 429 → :class:`ElevenLabsQuotaExceeded` propagates UNWRAPPED so
  the dispatcher records ``error.type == "ElevenLabsQuotaExceeded"`` (UC-9-E1).
  The atomic write in the client means NO ``narration.mp3`` is left behind, and
  the WhisperX alignment step is never reached, so NO ``words.json`` is written.

Cost (Slice-2 MINOR-2 invariant)
--------------------------------
ElevenLabs charges per minute of synthesized speech. Cost is recorded ONLY in
the returned ``StageResult.metrics["cost_usd"]`` and written into the manifest
by the dispatcher on the DONE transition — never mid-run, so a FAILED run
carries NO ``cost_usd``. ``next_call_cost_usd`` returns a pre-call ESTIMATE
(narration char count -> minutes x per-minute rate) for the dispatcher's cap
gate; the recorded cost uses the ACTUAL synthesized MP3 duration.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from shipcast.clients import ffmpeg_client as _ffmpeg
from shipcast.cost import ELEVENLABS_PER_MINUTE_USD
from shipcast.errors import StageInputMissing
from shipcast.manifest import StageStatus, dump_json_canonical
from shipcast.schemas import Storyboard, WordTimestamp
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from collections.abc import Callable

    from shipcast.project import Project

#: Rough spoken-words-per-minute used to ESTIMATE the pre-call cost from the
#: narration character count. Deliberately conservative (slow) so the estimate
#: errs toward over-charging the cap gate rather than under-charging it.
_WORDS_PER_MINUTE: float = 150.0
#: Average characters per spoken word (incl. trailing space) for the estimate.
_CHARS_PER_WORD: float = 6.0
#: The import name of the local alignment backend (the optional `whisperx`
#: extra installs `openai-whisper`, which imports as `whisper`). The backend
#: is a Python PACKAGE, not a CLI binary — so the pre-flight checks
#: importability, not `PATH`.
_WHISPER_PACKAGE: str = "whisper"


def _whisper_installed() -> bool:
    """True when the openai-whisper alignment backend is importable.

    Module-level so tests can monkeypatch it. The heavy package itself is NOT
    imported here — `find_spec` only checks availability.
    """
    return importlib.util.find_spec(_WHISPER_PACKAGE) is not None


# --------------------------------------------------------------------------- #
# Clients bundle Protocol (structural; mocked in tests)
# --------------------------------------------------------------------------- #


@runtime_checkable
class _ElevenLabsLike(Protocol):
    def synthesize_speech(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        *,
        model: str,
        voice_settings: dict[str, Any] | None = ...,
    ) -> Path: ...


@runtime_checkable
class _WhisperXLike(Protocol):
    def transcribe_with_alignment(
        self,
        mp3_path: Path,
        model_name: str,
        *,
        device: str = ...,
    ) -> list[WordTimestamp]: ...


class _ClientsBundle(Protocol):
    @property
    def elevenlabs(self) -> _ElevenLabsLike: ...

    @property
    def whisperx(self) -> _WhisperXLike: ...


def _default_clients_factory(project: Project) -> _ClientsBundle:
    """Construct the real ElevenLabs + WhisperX clients lazily inside ``run()``.

    Both heavy imports (the ElevenLabs SDK, openai-whisper/torch) live inside
    their respective client modules/methods, preserving CLI import-purity. The
    ElevenLabs client ``__init__`` validates the key; WhisperX takes no key.
    """
    from shipcast.clients.elevenlabs_client import ElevenLabsClient
    from shipcast.clients.whisperx_client import WhisperXClient

    elevenlabs = ElevenLabsClient(api_key=project.settings.elevenlabs_api_key)
    whisperx = WhisperXClient()

    class _Bundle:
        def __init__(self) -> None:
            self.elevenlabs: _ElevenLabsLike = elevenlabs
            self.whisperx: _WhisperXLike = whisperx

    return _Bundle()


class VoiceStage(BaseStage):
    """Produce ``07_voice/narration.mp3`` + ``07_voice/words.json``."""

    id: ClassVar[str] = "07_voice"
    requires: ClassVar[tuple[str, ...]] = ("05_script",)
    output_schema: ClassVar[type[Storyboard]] = Storyboard  # not the single-output
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Listen to narration.mp3 — confirm the pacing, tone, and pronunciation "
        "match the brand voice and the script reads naturally.",
        "Confirm narration.mp3 covers every beat's narration line with no "
        "missing or duplicated sentences.",
        "Spot-check words.json against the audio — the word timestamps should "
        "track the spoken words closely enough for caption timing.",
    )

    NARRATION_FILENAME: ClassVar[str] = "narration.mp3"
    WORDS_FILENAME: ClassVar[str] = "words.json"
    STORYBOARD_REL: ClassVar[str] = "05_script/storyboard.json"

    def __init__(
        self,
        *,
        clients_factory: Callable[[Project], _ClientsBundle] | None = None,
    ) -> None:
        self._clients_factory: Callable[[Project], _ClientsBundle] = (
            clients_factory or _default_clients_factory
        )

    # ------------------------------------------------------------- inputs
    def check_inputs(self, project: Project) -> None:
        """Default upstream gate PLUS a fail-fast whisper-backend pre-flight.

        The word-alignment step (FR-9.4) needs the ``openai-whisper`` package
        (the optional ``whisperx`` extra, imported as ``whisper``); if it is not
        importable we fail BEFORE any ElevenLabs synthesis is spent (UC-9-E3).
        The check runs FIRST (a cheap, side-effect-free ``find_spec`` — it does
        NOT import the heavy package) so the descriptive missing-tool error
        surfaces even when the upstream gate would also complain.
        """
        if not _whisper_installed():
            raise StageInputMissing(
                f"stage {self.id!r} requires the openai-whisper package "
                f"(imported as {_WHISPER_PACKAGE!r}) for word-timestamp "
                f"alignment; install it (e.g. `uv sync --extra whisperx`) and "
                f"rerun. Synthesis was NOT attempted."
            )
        super().check_inputs(project)

    def _load_storyboard(self, project: Project) -> Storyboard:
        path = project.path / self.STORYBOARD_REL
        if not path.is_file():
            raise StageInputMissing(
                f"stage {self.id!r} requires {self.STORYBOARD_REL} to exist"
            )
        return Storyboard.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _join_narration(storyboard: Storyboard) -> str:
        """Join beat narration lines with a single ``\\n``, no trailing newline."""
        return "\n".join(beat.narration for beat in storyboard.beats)

    @staticmethod
    def _voice_settings(project: Project) -> dict[str, Any]:
        s = project.settings
        return {
            "stability": s.voice_stability,
            "similarity_boost": s.voice_similarity_boost,
            "style": s.voice_style,
            "use_speaker_boost": s.voice_use_speaker_boost,
            "speed": s.voice_speed,
        }

    # ------------------------------------------------------------- cost gate
    def next_call_cost_usd(self, project: Project) -> float:
        """Pre-call ESTIMATE of the ElevenLabs per-minute charge.

        Estimated from the narration character count (chars → words → minutes),
        so the dispatcher's cap gate accounts for the synth BEFORE constructing
        the paid client. The DONE metric records the ACTUAL cost from the
        synthesized MP3 duration.
        """
        try:
            storyboard = self._load_storyboard(project)
        except StageInputMissing:
            return ELEVENLABS_PER_MINUTE_USD
        text = self._join_narration(storyboard)
        minutes = (len(text) / _CHARS_PER_WORD) / _WORDS_PER_MINUTE
        return round(ELEVENLABS_PER_MINUTE_USD * max(minutes, 0.0), 4)

    # ------------------------------------------------------------- run
    def run(self, project: Project) -> StageResult:
        clients = self._clients_factory(project)
        storyboard = self._load_storyboard(project)
        text = self._join_narration(storyboard)
        stage_dir = project.stage_dir(self.id)
        stage_dir.mkdir(parents=True, exist_ok=True)

        narration_path = stage_dir / self.NARRATION_FILENAME

        # 1) Synthesize. A 429 raises ElevenLabsQuotaExceeded here, BEFORE any
        #    artifact is written (the client's write is atomic), so a quota
        #    failure leaves no narration.mp3 and never reaches alignment.
        clients.elevenlabs.synthesize_speech(
            text,
            project.settings.voice_id,
            narration_path,
            model=project.settings.elevenlabs_model,
            voice_settings=self._voice_settings(project),
        )

        # 2) Align: local WhisperX-shaped client → non-empty list[WordTimestamp].
        words = clients.whisperx.transcribe_with_alignment(
            narration_path,
            project.settings.whisperx_model,
        )
        words_path = stage_dir / self.WORDS_FILENAME
        words_path.write_text(
            dump_json_canonical([w.model_dump(mode="json") for w in words]),
            encoding="utf-8",
        )

        # 3) Cost from the ACTUAL synthesized MP3 duration (per-minute rate).
        duration_sec = _ffmpeg.probe_audio_duration_sec(narration_path)
        minutes = (duration_sec or 0.0) / 60.0
        cost = round(ELEVENLABS_PER_MINUTE_USD * minutes, 4)

        outputs = (
            Path(self.id) / self.NARRATION_FILENAME,
            Path(self.id) / self.WORDS_FILENAME,
        )
        return StageResult(
            status=StageStatus.DONE,
            outputs=outputs,
            metrics={
                "cost_usd": cost,
                "duration_sec": round(duration_sec or 0.0, 3),
                "word_count": len(words),
            },
        )

    # ------------------------------------------------------------- outputs
    def validate_outputs(self, project: Project, result: StageResult) -> None:
        """Path-safety on both outputs + schema-check ``words.json`` only.

        The stage emits an MP3 plus a JSON list, so the default single-output
        schema check does not apply; we run the shared path-traversal guard over
        both outputs and validate ``words.json`` as a non-empty
        ``list[WordTimestamp]`` explicitly.
        """
        import json

        from pydantic import TypeAdapter, ValidationError

        from shipcast.errors import StageOutputInvalid

        self._validate_output_paths(project, result)
        words_rel = Path(self.id) / self.WORDS_FILENAME
        full = (project.path / words_rel).resolve()
        try:
            data = json.loads(full.read_text(encoding="utf-8"))
            parsed = TypeAdapter(list[WordTimestamp]).validate_python(data)
        except json.JSONDecodeError as exc:
            raise StageOutputInvalid(
                f"stage {self.id!r} output {full} is not valid JSON: {exc}"
            ) from exc
        except ValidationError as exc:
            raise StageOutputInvalid(
                f"stage {self.id!r} output {full} failed schema validation: {exc}"
            ) from exc
        if not parsed:
            raise StageOutputInvalid(
                f"stage {self.id!r} output {full} must be a non-empty word list"
            )
