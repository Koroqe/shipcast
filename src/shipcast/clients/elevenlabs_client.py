"""ElevenLabs API client — Stage 03 (`generate_voice`) uses this.

Constructed lazily inside `GenerateVoiceStage.run()` only (NFR-3.5: the
`elevenlabs` SDK import lives in THIS module and nowhere else, so the CLI
startup path never pulls it). Auth is via the operator's `ELEVENLABS_API_KEY`
SecretStr carried through `Settings`.

The SDK call is single-shot non-streaming (`text_to_speech.convert`), with a
fixed `seed=1` and `output_format="mp3_44100_128"` so that repeated runs on
the same script + voice + model produce byte-identical output (FR-3.6). The
write is atomic: bytes go to `<output>.mp3.tmp`, fsync, then `os.replace` to
the final path so a crash mid-write leaves the previous `narration.mp3` (if
any) untouched (FR-3.7a / REC-6).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import elevenlabs
from pydantic import SecretStr

from shipcast.errors import ElevenLabsQuotaExceeded, MissingApiKey

#: HTTP status the ElevenLabs API returns when the character quota is exhausted.
_QUOTA_STATUS: int = 429


class ElevenLabsClient:
    """Lazy ElevenLabs client. Construct inside `stage.run()` only."""

    #: Hardcoded output format. 44.1 kHz, 128 kbps mono MP3 — standard for
    #: spoken content. Not exposed as a config field in this feature (FR-3.5).
    OUTPUT_FORMAT: str = "mp3_44100_128"

    #: Fixed seed makes `--rerun` byte-identical when input is unchanged
    #: (FR-3.6). The ElevenLabs API treats `seed` as the deterministic-
    #: generation control.
    SEED: int = 1

    def __init__(self, api_key: SecretStr) -> None:
        if not api_key.get_secret_value():
            raise MissingApiKey("ELEVENLABS_API_KEY")
        self._api_key: SecretStr = api_key

    def __repr__(self) -> str:
        # Type name only — never expose key state.
        return f"<{type(self).__name__}>"

    def synthesize_speech(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        *,
        model: str,
        voice_settings: dict[str, Any] | None = None,
    ) -> Path:
        """Convert `text` to MP3 via ElevenLabs and write atomically to `output_path`.

        The SDK is constructed per call (FR-3.4) — no instance attribute caches
        the SDK handle, so the raw key is never persisted on `self`. The SDK
        returns an iterator of bytes chunks; we concatenate via `b"".join(...)`
        and write the full payload (FR-3.7 — non-streaming).

        Atomic write contract (FR-3.7a): bytes go to `<name>.mp3.tmp`, the file
        descriptor is `fsync`'d, then `os.replace` swaps the tmp into the final
        path. A crash between `write_bytes` and `os.replace` leaves only the
        `.tmp` file; the previous `output_path` (if any) is unmodified.

        Args:
            text: the narration text to synthesize (≤ 5000 chars enforced by
                the caller — FR-3.10).
            voice_id: ElevenLabs voice id (e.g., `"EXAVITQu4vr4xnSDxMaL"` for Sarah).
            output_path: target MP3 file path. Must be absolute; the stage
                supplies an absolute path (FR-3.16).
            model: ElevenLabs model id (FR-3.7) — keyword-only. Mapped to the
                SDK's `model_id` parameter.
            voice_settings: optional dict of ElevenLabs voice_settings
                (`stability`, `similarity_boost`, `style`, `use_speaker_boost`).
                When None, the SDK's per-voice defaults apply (backward-
                compatible with callers that pre-date this parameter). When
                supplied, the dict is passed verbatim into the SDK call.

        Returns:
            `output_path` (same value passed in, for caller convenience).

        Raises:
            ElevenLabsQuotaExceeded: HTTP 429 (character quota exhausted). The
                stage treats this as a HARD failure and writes no files
                (FR-9.5). The message names the quota status only — never the
                narration text.
            elevenlabs.core.ApiError: any OTHER HTTP error (401, 422, 402, etc.).
                Propagates unchanged (FR-3.9 — no retry, no wrapping).
            elevenlabs.core.UnauthorizedError: 401 specifically (subclass of ApiError).
            httpx.ConnectError / httpx.TimeoutException: network failures.
                Propagate unchanged.
            OSError: filesystem error during atomic write (rare; e.g., disk
                full between tmp write and `os.replace`).
        """
        sdk = elevenlabs.ElevenLabs(api_key=self._api_key.get_secret_value())
        convert_kwargs: dict[str, Any] = {
            "voice_id": voice_id,
            "text": text,
            "model_id": model,
            "output_format": self.OUTPUT_FORMAT,
            "seed": self.SEED,
        }
        if voice_settings is not None:
            convert_kwargs["voice_settings"] = voice_settings
        try:
            audio_iter = sdk.text_to_speech.convert(**convert_kwargs)
            audio_bytes = b"".join(audio_iter)
        except elevenlabs.core.ApiError as exc:
            # An HTTP 429 means the operator's character quota is exhausted.
            # Surface a typed quota error so the stage fails cleanly without
            # writing any partial artifact, and never echo the narration text.
            if getattr(exc, "status_code", None) == _QUOTA_STATUS:
                raise ElevenLabsQuotaExceeded(
                    "ElevenLabs character quota exhausted (HTTP 429); "
                    "wait for the quota window to reset and rerun."
                ) from exc
            raise

        tmp_path = output_path.with_suffix(".mp3.tmp")
        tmp_path.write_bytes(audio_bytes)
        with tmp_path.open("rb") as fd:
            os.fsync(fd.fileno())
        os.replace(tmp_path, output_path)
        return output_path
