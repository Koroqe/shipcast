"""Unit tests for ElevenLabsClient — covers lines 46, 50, 103-132.

Exercises:
- MissingApiKey when api_key is empty (TC-10.5 already in test_s07_voice_errors;
  we add more coverage of the synthesize_speech paths)
- Happy path: synthesize_speech writes MP3 atomically and returns output_path
- voice_settings=None → SDK called without voice_settings kwarg
- voice_settings supplied → forwarded to SDK
- HTTP 429 → ElevenLabsQuotaExceeded (no partial file left on disk)
- Other ApiError (e.g. 401) → propagates unchanged (not wrapped)
- __repr__ does NOT expose api key
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from shipcast.errors import ElevenLabsQuotaExceeded, MissingApiKey

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(key: str = "test-key-123") -> Any:
    """Import and instantiate ElevenLabsClient with a non-empty key."""
    from shipcast.clients.elevenlabs_client import ElevenLabsClient

    return ElevenLabsClient(api_key=SecretStr(key))


def _make_api_error(status_code: int) -> Any:
    """Create a fake elevenlabs.core.ApiError with the given status_code."""
    import elevenlabs.core

    err = elevenlabs.core.ApiError(status_code=status_code, body="error body")
    return err


# ---------------------------------------------------------------------------
# TC: MissingApiKey raised for empty key
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_for_empty_string() -> None:
    from shipcast.clients.elevenlabs_client import ElevenLabsClient

    with pytest.raises(MissingApiKey) as exc_info:
        ElevenLabsClient(api_key=SecretStr(""))
    assert "ELEVENLABS_API_KEY" in str(exc_info.value)


def test_missing_api_key_message_does_not_contain_value() -> None:
    from shipcast.clients.elevenlabs_client import ElevenLabsClient

    with pytest.raises(MissingApiKey) as exc_info:
        ElevenLabsClient(api_key=SecretStr(""))
    # The error message must name the key, not expose an empty value
    msg = str(exc_info.value)
    assert "ELEVENLABS_API_KEY" in msg


# ---------------------------------------------------------------------------
# TC: __repr__ does not expose the key
# ---------------------------------------------------------------------------


def test_repr_does_not_expose_key() -> None:
    client = _make_client("super-secret-key")
    r = repr(client)
    assert "super-secret-key" not in r
    assert "ElevenLabsClient" in r


# ---------------------------------------------------------------------------
# TC: happy path — atomic write, returns output_path, no voice_settings
# ---------------------------------------------------------------------------


def test_synthesize_speech_happy_path_no_voice_settings(tmp_path: Path) -> None:
    output_path = tmp_path / "narration.mp3"

    fake_sdk = MagicMock()
    fake_sdk.text_to_speech.convert.return_value = iter([b"FAKE", b"MP3"])

    with patch("elevenlabs.ElevenLabs", return_value=fake_sdk):
        client = _make_client()
        result = client.synthesize_speech(
            text="Hello world",
            voice_id="vid-001",
            output_path=output_path,
            model="eleven_v3",
        )

    assert result == output_path
    assert output_path.is_file()
    assert output_path.read_bytes() == b"FAKEMP3"

    # Tmp file is cleaned up after os.replace
    assert not output_path.with_suffix(".mp3.tmp").exists()

    # voice_settings NOT passed to SDK when None
    call_kwargs = fake_sdk.text_to_speech.convert.call_args.kwargs
    assert "voice_settings" not in call_kwargs


# ---------------------------------------------------------------------------
# TC: voice_settings dict forwarded to SDK when supplied
# ---------------------------------------------------------------------------


def test_synthesize_speech_forwards_voice_settings(tmp_path: Path) -> None:
    output_path = tmp_path / "narration.mp3"
    settings = {"stability": 0.5, "similarity_boost": 0.75}

    fake_sdk = MagicMock()
    fake_sdk.text_to_speech.convert.return_value = iter([b"bytes"])

    with patch("elevenlabs.ElevenLabs", return_value=fake_sdk):
        client = _make_client()
        client.synthesize_speech(
            text="Test",
            voice_id="vid-001",
            output_path=output_path,
            model="eleven_v3",
            voice_settings=settings,
        )

    call_kwargs = fake_sdk.text_to_speech.convert.call_args.kwargs
    assert call_kwargs["voice_settings"] == settings


# ---------------------------------------------------------------------------
# TC: HTTP 429 → ElevenLabsQuotaExceeded; no partial file left
# ---------------------------------------------------------------------------


def test_synthesize_speech_quota_429_raises_quota_exceeded(tmp_path: Path) -> None:
    output_path = tmp_path / "narration.mp3"

    fake_sdk = MagicMock()
    fake_sdk.text_to_speech.convert.side_effect = _make_api_error(429)

    with patch("elevenlabs.ElevenLabs", return_value=fake_sdk):
        client = _make_client()
        with pytest.raises(ElevenLabsQuotaExceeded):
            client.synthesize_speech(
                text="Text",
                voice_id="vid-001",
                output_path=output_path,
                model="eleven_v3",
            )

    # No partial file written
    assert not output_path.exists()
    assert not output_path.with_suffix(".mp3.tmp").exists()


def test_quota_exceeded_message_does_not_contain_narration_text(tmp_path: Path) -> None:
    output_path = tmp_path / "narration.mp3"
    secret_text = "VERY_SECRET_NARRATION_TEXT"

    fake_sdk = MagicMock()
    fake_sdk.text_to_speech.convert.side_effect = _make_api_error(429)

    with patch("elevenlabs.ElevenLabs", return_value=fake_sdk):
        client = _make_client()
        with pytest.raises(ElevenLabsQuotaExceeded) as exc_info:
            client.synthesize_speech(
                text=secret_text,
                voice_id="vid-001",
                output_path=output_path,
                model="eleven_v3",
            )

    assert secret_text not in str(exc_info.value)


# ---------------------------------------------------------------------------
# TC: Non-429 ApiError propagates unchanged (not wrapped as quota error)
# ---------------------------------------------------------------------------


def test_synthesize_speech_401_propagates_unchanged(tmp_path: Path) -> None:
    import elevenlabs.core

    output_path = tmp_path / "narration.mp3"

    fake_sdk = MagicMock()
    fake_sdk.text_to_speech.convert.side_effect = _make_api_error(401)

    with patch("elevenlabs.ElevenLabs", return_value=fake_sdk):
        client = _make_client()
        with pytest.raises(elevenlabs.core.ApiError) as exc_info:
            client.synthesize_speech(
                text="Text",
                voice_id="vid-001",
                output_path=output_path,
                model="eleven_v3",
            )
        # It should NOT have been re-raised as ElevenLabsQuotaExceeded
        assert not isinstance(exc_info.value, ElevenLabsQuotaExceeded)


# ---------------------------------------------------------------------------
# TC: SDK seed and output_format constants used
# ---------------------------------------------------------------------------


def test_synthesize_speech_uses_seed_and_output_format(tmp_path: Path) -> None:
    output_path = tmp_path / "narration.mp3"

    fake_sdk = MagicMock()
    fake_sdk.text_to_speech.convert.return_value = iter([b"x"])

    from shipcast.clients.elevenlabs_client import ElevenLabsClient

    with patch("elevenlabs.ElevenLabs", return_value=fake_sdk):
        client = _make_client()
        client.synthesize_speech(
            text="Hello",
            voice_id="vid-1",
            output_path=output_path,
            model="eleven_v3",
        )

    call_kwargs = fake_sdk.text_to_speech.convert.call_args.kwargs
    assert call_kwargs["seed"] == ElevenLabsClient.SEED
    assert call_kwargs["output_format"] == ElevenLabsClient.OUTPUT_FORMAT
