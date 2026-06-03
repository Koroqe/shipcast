"""Integration tests for `s07_voice` (Slice 14).

Owned TCs (Section 10):
- TC-10.1: happy path → `07_voice/narration.mp3` exists; `07_voice/words.json`
           is a non-empty list of `{word, start_sec, end_sec}`; the word-duration
           sum is within 1 s of the ffprobe MP3 duration.
- TC-10.2: the joined narration text == ``"A\nB\nC"`` (single ``\n``, no trailing).
- TC-10.3: ``synthesize_speech`` is called with ``Settings.voice_id``.

ElevenLabs synthesis + WhisperX transcription are ALWAYS mocked (no real API,
no real whisper model load). The synthesize mock writes a REAL ~3 s MP3 fixture's
bytes so ``ffprobe`` reads a real duration; the WhisperX mock returns word
timestamps whose ``[start, end]`` spans sum to within 1 s of that duration.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

import shipcast.cli as cli
import shipcast.schemas as schemas
import shipcast.stages.s02_enrich as enrich_mod
import shipcast.stages.s03_brand as brand_mod
import shipcast.stages.s04_plan as plan_mod
import shipcast.stages.s05_script as script_mod
import shipcast.stages.s07_voice as voice_mod
from shipcast.manifest import StageStatus
from shipcast.schemas import WordTimestamp

runner = CliRunner()

_REPO_FIXTURES = (
    Path(__file__).resolve().parent.parent / "fixtures" / "repos" / "example_min"
)
_CHANGELOG = (_REPO_FIXTURES / "CHANGELOG.md").read_text(encoding="utf-8")

#: A real ~3 s sine MP3 so ffprobe reports a genuine container duration.
_MP3_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "audio" / "narration_3s.mp3"
)
_MP3_BYTES = _MP3_FIXTURE.read_bytes()

REAL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

SLUG = "example-project--add-csv-export"
BRAND_SLUG = "test-brand"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repos_root"
    root.mkdir()
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", root)
    return root


@pytest.fixture
def target_repo(repo_root: Path) -> Path:
    repo = repo_root / "example-project"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def _reset_logging() -> Any:
    from shipcast import logging_setup

    logging_setup.reset_for_testing()
    yield
    logging_setup.reset_for_testing()


def _root(projects_root: Path) -> list[str]:
    return ["--projects-root", str(projects_root)]


def _seed_brand_pack(projects_root: Path) -> None:
    root = projects_root / "_brand" / BRAND_SLUG
    (root / "fonts").mkdir(parents=True, exist_ok=True)
    (root / "voice.md").write_text("# Voice\ncaption_mode: chip\n", encoding="utf-8")
    (root / "fonts" / "Inter.ttf").write_bytes(b"TTF-BYTES")
    (root / "logo.png").write_bytes(REAL_PNG)
    (root / "palette.hint.json").write_text(
        json.dumps({"primary": "#112233", "accent": "#445566", "neutral": "#778899"}),
        encoding="utf-8",
    )


def _valid_brief() -> dict[str, Any]:
    return {
        "hook_template_per_channel": {
            "x": "we_just_shipped",
            "linkedin": "before_after",
            "blog": "problem_aha",
        },
        "ctas": ["Try it now"],
        "video_beats": [
            {
                "image_prompt": f"beat {i} visual",
                "narration": f"beat {i} line",
                "duration_sec": 4.0,
            }
            for i in range(4)
        ],
        "carousel_beats": [
            {"headline": f"slide {i}", "body": f"body {i}"} for i in range(4)
        ],
        "has_stat_card": True,
        "has_code_screenshot": False,
    }


def _storyboard(narrations: list[str] | None = None) -> dict[str, Any]:
    lines = narrations or ["hero line", "fill line 1", "fill line 2", "fill line 3"]
    return {
        "beats": [
            {
                "image_prompt": f"beat {i} visual",
                "narration": line,
                "duration_sec": 4.0,
            }
            for i, line in enumerate(lines)
        ]
    }


def _drive_to_script_approved(
    projects_root: Path,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    narrations: list[str] | None = None,
) -> None:
    """pick → enrich → brand → plan → script (approving each) so s07's gate passes."""
    _real_subprocess_run = subprocess.run
    _seed_brand_pack(projects_root)

    result = runner.invoke(
        cli.app,
        [*_root(projects_root), "pick", str(target_repo), "--entry", "Add CSV export"],
    )
    assert result.exit_code == 0, result.output

    input_path = projects_root / SLUG / "input.yaml"
    data: dict[str, Any] = {
        "repo_path": str(target_repo),
        "entry_heading": "Add CSV export",
        "brand_slug": BRAND_SLUG,
        "video_mode": "standard",
    }
    input_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "01_pick"])
    assert result.exit_code == 0, result.output

    gemini = MagicMock()
    gemini.multimodal.return_value = "A compelling marketing narrative."

    def _enrich_factory(project: Any) -> Any:
        class _B:
            def __init__(self) -> None:
                self.gemini = gemini
                self.playwright = None

        return _B()

    monkeypatch.setattr(enrich_mod, "_default_clients_factory", _enrich_factory)

    _real_run = subprocess.run

    def _fake_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        prog = Path(cmd[0]).name
        if prog in ("gh", "git"):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if prog == "claude":
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        if prog in ("ffmpeg", "ffprobe"):
            return _real_run(cmd, *a, **k)
        raise AssertionError(f"unexpected subprocess: {cmd!r}")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli.app, [*_root(projects_root), "approve", SLUG, "02_enrich"]
    )
    assert result.exit_code == 0, result.output

    brand_gemini = MagicMock()
    brand_gemini.generate_image.return_value = REAL_PNG

    def _brand_factory(project: Any) -> Any:
        class _B:
            def __init__(self) -> None:
                self.gemini = brand_gemini
                self.playwright = MagicMock()

        return _B()

    monkeypatch.setattr(brand_mod, "_default_clients_factory", _brand_factory)
    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "03_brand"])
    assert result.exit_code == 0, result.output

    def _plan_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        assert cmd[0] == "claude", f"unexpected subprocess: {cmd!r}"
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(_valid_brief()), stderr=""
        )

    monkeypatch.setattr(plan_mod.subprocess, "run", _plan_run)
    result = runner.invoke(cli.app, [*_root(projects_root), "plan", SLUG])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "04_plan"])
    assert result.exit_code == 0, result.output

    def _script_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        assert cmd[0] == "claude", f"unexpected subprocess: {cmd!r}"
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(_storyboard(narrations)), stderr=""
        )

    monkeypatch.setattr(script_mod.subprocess, "run", _script_run)
    result = runner.invoke(cli.app, [*_root(projects_root), "script", SLUG])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "05_script"])
    assert result.exit_code == 0, result.output

    monkeypatch.setattr(subprocess, "run", _real_subprocess_run)


def _voice_dir(projects_root: Path) -> Path:
    return projects_root / SLUG / "07_voice"


def _manifest(projects_root: Path) -> Any:
    from shipcast.manifest import Manifest

    return Manifest.load(projects_root / SLUG / "manifest.json")


# --------------------------------------------------------------------------- #
# Mock client bundle installer
# --------------------------------------------------------------------------- #


def _install_clients(
    monkeypatch: pytest.MonkeyPatch, *, elevenlabs: Any, whisperx: Any
) -> None:
    # Make the `whisperx` PATH pre-flight pass (the binary is not installed in
    # CI); the actual alignment client is mocked, so no real model loads.
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/local/bin/whisperx" if name == "whisperx" else None
    )

    def _factory(project: Any) -> Any:
        class _B:
            def __init__(self) -> None:
                self.elevenlabs = elevenlabs
                self.whisperx = whisperx

        return _B()

    monkeypatch.setattr(voice_mod, "_default_clients_factory", _factory)


def _make_elevenlabs() -> MagicMock:
    """ElevenLabs mock whose synthesize_speech writes the real 3 s MP3 bytes."""
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
        output_path.write_bytes(_MP3_BYTES)
        return output_path

    el.synthesize_speech.side_effect = _synth
    return el


def _make_whisperx(words: list[WordTimestamp]) -> MagicMock:
    wx = MagicMock()
    wx.transcribe_with_alignment.return_value = words
    return wx


# --------------------------------------------------------------------------- #
# TC-10.1 — happy path: narration.mp3 + words.json; duration-sum tolerance
# --------------------------------------------------------------------------- #


def test_tc_10_1_happy_path_mp3_and_words(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch)

    # Word spans that cover the real 3.0 s MP3 closely (sum of span durations
    # within 1 s of the ffprobe MP3 duration). 6 words @ 0.5 s each = 3.0 s.
    words = [
        WordTimestamp(
            word=f"w{i}",
            start_sec=i * 0.5,
            end_sec=(i + 1) * 0.5,
            confidence=0.9,
        )
        for i in range(6)
    ]
    el = _make_elevenlabs()
    wx = _make_whisperx(words)
    _install_clients(monkeypatch, elevenlabs=el, whisperx=wx)

    result = runner.invoke(cli.app, [*_root(projects_root), "voice", SLUG])
    assert result.exit_code == 0, result.output

    vd = _voice_dir(projects_root)
    mp3 = vd / "narration.mp3"
    words_json = vd / "words.json"
    assert mp3.is_file(), "narration.mp3 missing"
    assert words_json.is_file(), "words.json missing"

    parsed = json.loads(words_json.read_text(encoding="utf-8"))
    assert isinstance(parsed, list) and parsed, "words.json must be a non-empty list"
    for entry in parsed:
        assert {"word", "start_sec", "end_sec"} <= set(entry)

    # The real MP3 fixture is ~3 s; ffprobe reports its true duration.
    from shipcast.clients.ffmpeg_client import probe_video

    mp3_duration = probe_video(mp3).duration_sec
    assert mp3_duration is not None
    span_sum = sum(e["end_sec"] - e["start_sec"] for e in parsed)
    assert abs(span_sum - mp3_duration) <= 1.0, (
        f"word-duration sum {span_sum} not within 1 s of MP3 duration {mp3_duration}"
    )

    record = _manifest(projects_root).stages["07_voice"]
    assert record.status == StageStatus.DONE
    # Cost recorded on DONE: ElevenLabs per-minute charge for the synth.
    assert "cost_usd" in record.metrics
    assert record.metrics["cost_usd"] > 0


# --------------------------------------------------------------------------- #
# TC-10.2 — narration script joins beat narrations with single newlines
# --------------------------------------------------------------------------- #


def test_tc_10_2_joined_text_single_newlines(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(
        projects_root, target_repo, monkeypatch, narrations=["A", "B", "C", "D"]
    )

    captured: dict[str, str] = {}

    el = MagicMock()

    def _synth(
        text: str,
        voice_id: str,
        output_path: Path,
        *,
        model: str,
        voice_settings: Any = None,
    ) -> Path:
        captured["text"] = text
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(_MP3_BYTES)
        return output_path

    el.synthesize_speech.side_effect = _synth
    wx = _make_whisperx(
        [WordTimestamp(word="x", start_sec=0.0, end_sec=3.0, confidence=0.9)]
    )
    _install_clients(monkeypatch, elevenlabs=el, whisperx=wx)

    result = runner.invoke(cli.app, [*_root(projects_root), "voice", SLUG])
    assert result.exit_code == 0, result.output

    assert captured["text"] == "A\nB\nC\nD"
    assert not captured["text"].endswith("\n")


# --------------------------------------------------------------------------- #
# TC-10.3 — voice_id read from Settings.voice_id, not from voice.md
# --------------------------------------------------------------------------- #


def test_tc_10_3_voice_id_from_settings(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch)

    from shipcast.config import Settings

    real_from_files = Settings.from_files.__func__  # type: ignore[attr-defined]

    def _patched_from_files(cls: Any, *a: Any, **k: Any) -> Settings:
        settings = real_from_files(cls, *a, **k)
        return settings.model_copy(update={"voice_id": "test-voice-id"})

    monkeypatch.setattr(Settings, "from_files", classmethod(_patched_from_files))

    el = _make_elevenlabs()
    wx = _make_whisperx(
        [WordTimestamp(word="x", start_sec=0.0, end_sec=3.0, confidence=0.9)]
    )
    _install_clients(monkeypatch, elevenlabs=el, whisperx=wx)

    result = runner.invoke(cli.app, [*_root(projects_root), "voice", SLUG])
    assert result.exit_code == 0, result.output

    _args, kwargs = el.synthesize_speech.call_args
    # voice_id is the 2nd positional arg per the client signature.
    assert el.synthesize_speech.call_args.args[1] == "test-voice-id" or (
        kwargs.get("voice_id") == "test-voice-id"
    )
