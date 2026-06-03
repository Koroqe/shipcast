"""Unit error tests for `s07_voice` (Slice 14).

Owned TCs (Section 10):
- TC-10.4: ElevenLabs 429 → ``ElevenLabsQuotaExceeded`` raised; the stage produces
           NO ``narration.mp3`` and NO ``words.json``.
- TC-10.5: constructing ``ElevenLabsClient`` with an empty key →
           ``MissingApiKey("ELEVENLABS_API_KEY")`` whose message names the KEY
           only, never the empty value.
- TC-10.6: ``whisperx`` absent from PATH → ``check_inputs`` fails with a
           descriptive error BEFORE any synthesis (the ElevenLabs client is never
           even constructed).

No real ElevenLabs / WhisperX calls; the WhisperX model is never instantiated.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import shipcast.stages.s07_voice as voice_mod
from shipcast.errors import ElevenLabsQuotaExceeded, MissingApiKey, StageInputMissing
from shipcast.schemas import WordTimestamp

# --------------------------------------------------------------------------- #
# A minimal Project double + storyboard seed
# --------------------------------------------------------------------------- #


class _FakeSettings:
    voice_id = "vid-123"
    elevenlabs_model = "eleven_v3"
    whisperx_model = "base.en"
    voice_stability = 0.2
    voice_similarity_boost = 0.85
    voice_style = 0.8
    voice_use_speaker_boost = True
    voice_speed = 1.2


class _FakeProject:
    def __init__(self, root: Path) -> None:
        self.path = root
        self.settings = _FakeSettings()

    def stage_dir(self, stage_id: str) -> Path:
        return self.path / stage_id

    def artifact_path(self, stage_id: str, name: str) -> Path:
        return self.stage_dir(stage_id) / name


def _seed_storyboard(root: Path, narrations: list[str]) -> None:
    sb_dir = root / "05_script"
    sb_dir.mkdir(parents=True, exist_ok=True)
    beats = [
        {"image_prompt": f"p{i}", "narration": line, "duration_sec": 4.0}
        for i, line in enumerate(narrations)
    ]
    import json

    (sb_dir / "storyboard.json").write_text(
        json.dumps({"beats": beats}), encoding="utf-8"
    )


def _install_clients(
    monkeypatch: pytest.MonkeyPatch, *, elevenlabs: Any, whisperx: Any
) -> None:
    def _factory(project: Any) -> Any:
        class _B:
            def __init__(self) -> None:
                self.elevenlabs = elevenlabs
                self.whisperx = whisperx

        return _B()

    monkeypatch.setattr(voice_mod, "_default_clients_factory", _factory)


# --------------------------------------------------------------------------- #
# TC-10.4 — ElevenLabsQuotaExceeded on 429 → no files written
# --------------------------------------------------------------------------- #


def test_tc_10_4_quota_exceeded_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_storyboard(tmp_path, ["A", "B", "C", "D"])
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/whisperx")

    el = MagicMock()
    el.synthesize_speech.side_effect = ElevenLabsQuotaExceeded()
    wx = MagicMock()
    _install_clients(monkeypatch, elevenlabs=el, whisperx=wx)

    project = _FakeProject(tmp_path)
    stage = voice_mod.VoiceStage()
    with pytest.raises(ElevenLabsQuotaExceeded):
        stage.run(project)  # type: ignore[arg-type]

    vd = tmp_path / "07_voice"
    assert not (vd / "narration.mp3").exists()
    assert not (vd / "words.json").exists()
    # WhisperX is never reached when synthesis fails.
    wx.transcribe_with_alignment.assert_not_called()


# --------------------------------------------------------------------------- #
# TC-10.5 — ElevenLabs auth error (MissingApiKey, name only)
# --------------------------------------------------------------------------- #


def test_tc_10_5_missing_api_key_name_only() -> None:
    from pydantic import SecretStr

    from shipcast.clients.elevenlabs_client import ElevenLabsClient

    with pytest.raises(MissingApiKey) as exc_info:
        ElevenLabsClient(api_key=SecretStr(""))

    message = str(exc_info.value)
    assert "ELEVENLABS_API_KEY" in message


# --------------------------------------------------------------------------- #
# TC-10.6 — whisperx not on PATH → fail before synthesis
# --------------------------------------------------------------------------- #


def test_tc_10_6_whisperx_missing_fails_before_synth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_storyboard(tmp_path, ["A", "B", "C", "D"])
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    el = MagicMock()
    wx = MagicMock()
    _install_clients(monkeypatch, elevenlabs=el, whisperx=wx)

    project = _FakeProject(tmp_path)
    stage = voice_mod.VoiceStage()

    with pytest.raises(StageInputMissing) as exc_info:
        stage.check_inputs(project)  # type: ignore[arg-type]

    assert "whisperx" in str(exc_info.value).lower()
    # Synthesis API never touched.
    el.synthesize_speech.assert_not_called()


# --------------------------------------------------------------------------- #
# Helper coverage: the joined-narration + transcribe wiring on the happy path
# (no real clients — confirms run() returns a DONE result with cost recorded).
# --------------------------------------------------------------------------- #


def test_run_records_cost_and_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_storyboard(tmp_path, ["A", "B", "C", "D"])
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/whisperx")

    el = MagicMock()

    def _synth(
        text: str,
        voice_id: str,
        output_path: Path,
        *,
        model: str,
        voice_settings: Any = None,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"FAKE-MP3-BYTES")
        return output_path

    el.synthesize_speech.side_effect = _synth
    wx = MagicMock()
    wx.transcribe_with_alignment.return_value = [
        WordTimestamp(word="A", start_sec=0.0, end_sec=1.0, confidence=0.9)
    ]
    _install_clients(monkeypatch, elevenlabs=el, whisperx=wx)

    stage = voice_mod.VoiceStage()
    result = stage.run(_FakeProject(tmp_path))  # type: ignore[arg-type]

    from shipcast.manifest import StageStatus

    assert result.status == StageStatus.DONE
    # The fake (non-MP3) bytes have no probeable duration, so the per-minute
    # cost rounds to 0.0; the real-duration cost is asserted in the integration
    # happy-path test with the genuine MP3 fixture. The metric key MUST exist.
    assert "cost_usd" in result.metrics
    assert result.metrics["word_count"] == 1
    el.synthesize_speech.assert_called_once()
    wx.transcribe_with_alignment.assert_called_once()
    # narration.mp3 and words.json both written.
    assert (tmp_path / "07_voice" / "narration.mp3").is_file()
    assert (tmp_path / "07_voice" / "words.json").is_file()
