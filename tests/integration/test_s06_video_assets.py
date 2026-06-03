"""Integration + unit tests for `s06_video_assets` (Slice 13, SECURITY).

Owned TCs (Section 9):
- TC-9.1: standard → 4 clips 1080x1920 h264 3-5 s; Veo never called.
- TC-9.2: premium → beat[0] = 8 s Veo clip; beats[1..3] = Ken-Burns 3-5 s;
          Veo called exactly once.
- TC-9.3: VeoSafetyBlocked on beat[0] → Ken-Burns fallback; blocked prompt
          absent from all log files (SECURITY no-leak).
- TC-9.4: VeoQuotaExceeded → stage FAILED; beats[1..3] not written.
- TC-9.5: VeoTimeout → stage FAILED.
- TC-9.6: --no-veo premium → all Ken-Burns; Veo never called.
- TC-9.7: ffprobe reports a bad codec → ClipValidationFailed (structured).
- TC-9.8: Imagen GeminiRateLimited (transient, retries exhausted) → FAILED.
- GAP: Imagen safety block → error.type == "GeminiSafetyBlocked".

Gemini Imagen + Veo are ALWAYS mocked (no real API). ffmpeg/ffprobe are real for
the standard happy-path clip render; the Veo / fallback / error paths mock the
ffmpeg Ken-Burns helper so those tests stay fast.
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
import shipcast.stages.s06_video_assets as va_mod
from shipcast.errors import (
    GeminiSafetyBlocked,
    GeminiTransientError,
    VeoQuotaExceeded,
    VeoSafetyBlocked,
    VeoTimeout,
)
from shipcast.manifest import StageStatus
from shipcast.schemas import VideoBeats

runner = CliRunner()

_REPO_FIXTURES = (
    Path(__file__).resolve().parent.parent / "fixtures" / "repos" / "example_min"
)
_CHANGELOG = (_REPO_FIXTURES / "CHANGELOG.md").read_text(encoding="utf-8")

REAL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

SLUG = "example-project--add-csv-export"
BRAND_SLUG = "test-brand"

_HERO_PROMPT = "SECRET-HERO-PROMPT-must-not-leak-to-logs-998877"


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
    """Force per-test log reconfiguration so each project gets its own log file."""
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


def _storyboard(*, hero_prompt: str = _HERO_PROMPT) -> dict[str, Any]:
    """A 4-beat storyboard; beat[0] carries the (sensitive) hero prompt."""
    beats = [
        {"image_prompt": hero_prompt, "narration": "hero line", "duration_sec": 4.0},
    ]
    for i in range(1, 4):
        beats.append(
            {
                "image_prompt": f"fill beat {i}",
                "narration": f"fill line {i}",
                "duration_sec": 4.0,
            }
        )
    return {"beats": beats}


def _drive_to_script_approved(
    projects_root: Path,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    video_mode: str = "standard",
) -> None:
    """pick → enrich → brand → plan → script, approving each, so s06's gate passes.

    The enrich/plan/script fakes monkeypatch the SHARED ``subprocess`` module's
    ``run`` (``plan_mod.subprocess`` / ``script_mod.subprocess`` are the same
    module object), so the last patch would leak into the Stage-06 dispatch and
    break its real ``ffmpeg -version`` pre-flight. We capture the real ``run``
    here and restore it at the end so Stage 06 sees a clean subprocess.
    """
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
        "video_mode": video_mode,
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

    # Capture the REAL subprocess.run so the global patch can delegate the
    # ffmpeg/ffprobe pre-flight + Ken-Burns renders (which Stage 06 shells out to
    # for real) while still faking gh/git/claude. The patch persists past enrich,
    # so without this passthrough the Stage-06 `ffmpeg -version` pre-flight would
    # hit the fake and abort.
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
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "02_enrich"])
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
            cmd, 0, stdout=json.dumps(_storyboard()), stderr=""
        )

    monkeypatch.setattr(script_mod.subprocess, "run", _script_run)
    result = runner.invoke(cli.app, [*_root(projects_root), "script", SLUG])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "05_script"])
    assert result.exit_code == 0, result.output

    # Restore the real subprocess.run so Stage 06's ffmpeg pre-flight + real
    # Ken-Burns/ffprobe calls are not shadowed by the script-stage fake.
    monkeypatch.setattr(subprocess, "run", _real_subprocess_run)


def _va_dir(projects_root: Path) -> Path:
    return projects_root / SLUG / "06_video_assets"


def _manifest(projects_root: Path) -> Any:
    from shipcast.manifest import Manifest

    return Manifest.load(projects_root / SLUG / "manifest.json")


def _set_premium_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the loaded Settings report premium mode so the $8 cap applies.

    `project.settings` is rebuilt from config.toml on every load, so the cost cap
    derives from config.toml's default mode (standard → $3). Premium tests patch
    `Settings.from_files` to return a premium-mode Settings so the dispatcher's
    pre-call gate uses the $8 cap. The per-project render mode is read from
    input.yaml independently.
    """
    from shipcast.config import Settings

    real_from_files = Settings.from_files.__func__  # type: ignore[attr-defined]

    def _premium_from_files(cls: Any, *a: Any, **k: Any) -> Settings:
        settings = real_from_files(cls, *a, **k)
        return settings.model_copy(update={"video_mode": "premium"})

    monkeypatch.setattr(Settings, "from_files", classmethod(_premium_from_files))


# --------------------------------------------------------------------------- #
# Imagen + Veo mock factories installed on the stage
# --------------------------------------------------------------------------- #


def _install_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gemini: Any,
    veo: Any,
) -> None:
    def _factory(project: Any) -> Any:
        class _B:
            def __init__(self) -> None:
                self.gemini = gemini
                self.veo = veo

        return _B()

    monkeypatch.setattr(va_mod, "_default_clients_factory", _factory)


def _mock_kenburns(monkeypatch: pytest.MonkeyPatch, *, durations: dict[int, float]) -> MagicMock:
    """Replace the real ffmpeg Ken-Burns render with a tiny stub MP4 writer."""
    calls = MagicMock()

    def _fake_kb(*, still_path: Path, duration_sec: float, output_path: Path, fast: bool = False) -> Path:
        calls(output_path.name, duration_sec)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x00\x00\x00\x18ftypmp42KEN-BURNS-STUB")
        return output_path

    monkeypatch.setattr(va_mod._ffmpeg, "ken_burns_clip", _fake_kb)
    return calls


def _mock_probe_ok(monkeypatch: pytest.MonkeyPatch, *, durations: dict[str, float]) -> None:
    """probe_video returns h264 1080x1920 with per-file durations."""
    from shipcast.clients.ffmpeg_client import ProbeResult

    def _fake_probe(path: Path) -> ProbeResult:
        return ProbeResult(
            codec_name="h264",
            width=1080,
            height=1920,
            duration_sec=durations.get(path.name, 4.0),
        )

    monkeypatch.setattr(va_mod._ffmpeg, "probe_video", _fake_probe)


# --------------------------------------------------------------------------- #
# TC-9.1 — standard mode, REAL ffmpeg
# --------------------------------------------------------------------------- #


def test_tc_9_1_standard_four_real_clips(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch, video_mode="standard")

    gemini = MagicMock()
    gemini.generate_image.return_value = _solid_png()
    veo = MagicMock()
    veo.generate_clip.side_effect = AssertionError("Veo must NOT be called in standard mode")
    _install_clients(monkeypatch, gemini=gemini, veo=veo)

    result = runner.invoke(cli.app, [*_root(projects_root), "video_assets", SLUG])
    assert result.exit_code == 0, result.output

    va = _va_dir(projects_root)
    from shipcast.clients.ffmpeg_client import probe_video

    for i in range(4):
        clip = va / f"beat_{i:02d}.mp4"
        assert clip.is_file(), f"missing {clip}"
        probe = probe_video(clip)
        assert probe.codec_name == "h264"
        assert (probe.width, probe.height) == (1080, 1920)
        assert 2.8 <= (probe.duration_sec or 0) <= 5.2

    veo.generate_clip.assert_not_called()

    clips = VideoBeats.model_validate_json((va / "clips.json").read_text())
    assert clips.mode == "standard"
    assert [c.source for c in clips.clips] == ["ken_burns"] * 4

    record = _manifest(projects_root).stages["06_video_assets"]
    assert record.status == StageStatus.DONE
    assert record.metrics["cost_usd"] == pytest.approx(4 * 0.04)


def _solid_png() -> bytes:
    """A small solid-colour PNG the Ken-Burns ffmpeg render accepts."""
    import struct
    import zlib

    w = h = 64
    raw = b""
    for _ in range(h):
        raw += b"\x00" + bytes([200, 40, 40] * w)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(raw))
    png += _chunk(b"IEND", b"")
    return png


# --------------------------------------------------------------------------- #
# TC-9.2 — premium mode, Veo hero + Ken-Burns fills (ffmpeg mocked)
# --------------------------------------------------------------------------- #


def test_tc_9_2_premium_veo_hero(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch, video_mode="premium")
    _set_premium_cap(monkeypatch)

    gemini = MagicMock()
    gemini.generate_image.return_value = _solid_png()

    veo = MagicMock()

    def _veo_clip(prompt: str, *, model: str, output_path: Path, conditioning_image: Any = None) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x00\x00\x00\x18ftypmp42VEO-8S-CLIP")
        return output_path

    veo.generate_clip.side_effect = _veo_clip
    _install_clients(monkeypatch, gemini=gemini, veo=veo)

    _mock_kenburns(monkeypatch, durations={})
    _mock_probe_ok(
        monkeypatch,
        durations={"beat_00.mp4": 8.0, "beat_01.mp4": 4.0, "beat_02.mp4": 4.0, "beat_03.mp4": 4.0},
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "video_assets", SLUG])
    assert result.exit_code == 0, result.output

    assert veo.generate_clip.call_count == 1
    # fills use Imagen (3 calls)
    assert gemini.generate_image.call_count == 3

    clips = VideoBeats.model_validate_json((_va_dir(projects_root) / "clips.json").read_text())
    assert clips.mode == "premium"
    assert clips.clips[0].source == "veo"
    assert clips.clips[0].duration_sec == pytest.approx(8.0)
    assert [c.source for c in clips.clips[1:]] == ["ken_burns"] * 3

    record = _manifest(projects_root).stages["06_video_assets"]
    assert record.status == StageStatus.DONE
    assert record.metrics["cost_usd"] == pytest.approx(3.20 + 3 * 0.04)


# --------------------------------------------------------------------------- #
# TC-9.3 — VeoSafetyBlocked fallback + SECURITY no-leak
# --------------------------------------------------------------------------- #


def test_tc_9_3_safety_block_fallback_no_prompt_leak(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch, video_mode="premium")
    _set_premium_cap(monkeypatch)

    gemini = MagicMock()
    gemini.generate_image.return_value = _solid_png()
    veo = MagicMock()
    veo.generate_clip.side_effect = VeoSafetyBlocked("Celebrity likeness")
    _install_clients(monkeypatch, gemini=gemini, veo=veo)

    kb_calls = _mock_kenburns(monkeypatch, durations={})
    _mock_probe_ok(monkeypatch, durations={})

    result = runner.invoke(cli.app, [*_root(projects_root), "video_assets", SLUG])
    assert result.exit_code == 0, result.output

    # beat[0] produced via Ken-Burns fallback → 4 Ken-Burns renders total.
    rendered = {args[0][0] for args in kb_calls.call_args_list}
    assert "beat_00.mp4" in rendered
    assert (_va_dir(projects_root) / "beat_00.mp4").is_file()

    clips = VideoBeats.model_validate_json((_va_dir(projects_root) / "clips.json").read_text())
    assert clips.clips[0].source == "ken_burns"

    record = _manifest(projects_root).stages["06_video_assets"]
    assert record.status == StageStatus.DONE
    # fallback cost: 4 Imagen stills, no Veo charge.
    assert record.metrics["cost_usd"] == pytest.approx(4 * 0.04)

    # SECURITY: the blocked hero prompt must not appear in ANY log file.
    logs_dir = projects_root / SLUG / "logs"
    leaked = []
    for log_file in logs_dir.glob("*.log"):
        if _HERO_PROMPT in log_file.read_text(encoding="utf-8", errors="replace"):
            leaked.append(log_file.name)
    assert not leaked, f"hero prompt leaked into log files: {leaked}"


# --------------------------------------------------------------------------- #
# TC-9.4 — VeoQuotaExceeded → FAILED, no further beats, NO cost recorded
# --------------------------------------------------------------------------- #


def test_tc_9_4_quota_exceeded_fails(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch, video_mode="premium")
    _set_premium_cap(monkeypatch)

    gemini = MagicMock()
    gemini.generate_image.return_value = _solid_png()
    veo = MagicMock()
    veo.generate_clip.side_effect = VeoQuotaExceeded("quota")
    _install_clients(monkeypatch, gemini=gemini, veo=veo)

    kb_calls = _mock_kenburns(monkeypatch, durations={})
    _mock_probe_ok(monkeypatch, durations={})

    result = runner.invoke(cli.app, [*_root(projects_root), "video_assets", SLUG])
    print("VA-OUT", result.exit_code, result.output)
    assert result.exit_code != 0

    record = _manifest(projects_root).stages["06_video_assets"]
    assert record.status == StageStatus.FAILED, result.output
    assert record.error is not None
    assert record.error.type == "VeoQuotaExceeded"
    # MINOR-2 invariant: a FAILED paid run records NO cost_usd.
    assert "cost_usd" not in record.metrics

    va = _va_dir(projects_root)
    for i in range(1, 4):
        assert not (va / f"beat_{i:02d}.mp4").exists()
    # fill beats never rendered after the hero quota failure.
    kb_calls.assert_not_called()


# --------------------------------------------------------------------------- #
# TC-9.5 — VeoTimeout → FAILED
# --------------------------------------------------------------------------- #


def test_tc_9_5_veo_timeout_fails(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch, video_mode="premium")
    _set_premium_cap(monkeypatch)

    gemini = MagicMock()
    gemini.generate_image.return_value = _solid_png()
    veo = MagicMock()
    veo.generate_clip.side_effect = VeoTimeout("timeout")
    _install_clients(monkeypatch, gemini=gemini, veo=veo)
    _mock_kenburns(monkeypatch, durations={})
    _mock_probe_ok(monkeypatch, durations={})

    result = runner.invoke(cli.app, [*_root(projects_root), "video_assets", SLUG])
    assert result.exit_code != 0

    record = _manifest(projects_root).stages["06_video_assets"]
    assert record.status == StageStatus.FAILED
    assert record.error is not None
    assert record.error.type == "VeoTimeout"
    assert "cost_usd" not in record.metrics


# --------------------------------------------------------------------------- #
# TC-9.6 — --no-veo premium → all Ken-Burns; Veo never called
# --------------------------------------------------------------------------- #


def test_tc_9_6_no_veo_flag(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch, video_mode="premium")
    _set_premium_cap(monkeypatch)

    gemini = MagicMock()
    gemini.generate_image.return_value = _solid_png()
    veo = MagicMock()
    veo.generate_clip.side_effect = AssertionError("Veo must NOT be called with --no-veo")
    _install_clients(monkeypatch, gemini=gemini, veo=veo)

    kb_calls = _mock_kenburns(monkeypatch, durations={})
    _mock_probe_ok(monkeypatch, durations={})

    result = runner.invoke(cli.app, [*_root(projects_root), "video_assets", SLUG, "--no-veo"])
    assert result.exit_code == 0, result.output

    veo.generate_clip.assert_not_called()
    rendered = {args[0][0] for args in kb_calls.call_args_list}
    assert rendered == {f"beat_{i:02d}.mp4" for i in range(4)}

    clips = VideoBeats.model_validate_json((_va_dir(projects_root) / "clips.json").read_text())
    assert [c.source for c in clips.clips] == ["ken_burns"] * 4

    record = _manifest(projects_root).stages["06_video_assets"]
    assert record.metrics["cost_usd"] == pytest.approx(4 * 0.04)


# --------------------------------------------------------------------------- #
# TC-9.7 — ffprobe reports a bad codec → ClipValidationFailed
# --------------------------------------------------------------------------- #


def test_tc_9_7_bad_codec_validation(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch, video_mode="standard")

    gemini = MagicMock()
    gemini.generate_image.return_value = _solid_png()
    veo = MagicMock()
    _install_clients(monkeypatch, gemini=gemini, veo=veo)

    _mock_kenburns(monkeypatch, durations={})

    from shipcast.clients.ffmpeg_client import ProbeResult

    def _bad_probe(path: Path) -> ProbeResult:
        if path.name == "beat_00.mp4":
            return ProbeResult(codec_name="vp9", width=1080, height=1920, duration_sec=4.0)
        return ProbeResult(codec_name="h264", width=1080, height=1920, duration_sec=4.0)

    monkeypatch.setattr(va_mod._ffmpeg, "probe_video", _bad_probe)

    result = runner.invoke(cli.app, [*_root(projects_root), "video_assets", SLUG])
    assert result.exit_code != 0

    record = _manifest(projects_root).stages["06_video_assets"]
    assert record.status == StageStatus.FAILED
    assert record.error is not None
    assert record.error.type == "ClipValidationFailed"
    assert "beat_00.mp4" in record.error.message
    assert "cost_usd" not in record.metrics


# --------------------------------------------------------------------------- #
# TC-9.8 — Imagen rate-limit (transient, retries exhausted) → FAILED
# --------------------------------------------------------------------------- #


def test_tc_9_8_imagen_rate_limit_fails(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch, video_mode="standard")

    gemini = MagicMock()
    # First still ok, second still raises transient on every retry attempt.
    state = {"calls": 0}

    def _gen(prompt: str, *, model: str, seed: int, aspect_ratio: str = "16:9") -> bytes:
        state["calls"] += 1
        if state["calls"] == 1:
            return _solid_png()
        raise GeminiTransientError(429, "rate limited")

    gemini.generate_image.side_effect = _gen
    veo = MagicMock()
    _install_clients(monkeypatch, gemini=gemini, veo=veo)
    _mock_kenburns(monkeypatch, durations={})
    _mock_probe_ok(monkeypatch, durations={})

    result = runner.invoke(cli.app, [*_root(projects_root), "video_assets", SLUG])
    assert result.exit_code != 0

    record = _manifest(projects_root).stages["06_video_assets"]
    assert record.status == StageStatus.FAILED
    assert record.error is not None
    assert record.error.type == "GeminiImageGenFailed"
    assert "cost_usd" not in record.metrics


# --------------------------------------------------------------------------- #
# GAP — Imagen safety block surfaces error.type == "GeminiSafetyBlocked"
# --------------------------------------------------------------------------- #


def test_gap_imagen_safety_block_subtype(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive_to_script_approved(projects_root, target_repo, monkeypatch, video_mode="standard")

    gemini = MagicMock()
    gemini.generate_image.side_effect = GeminiSafetyBlocked(200, "content policy block: SAFETY")
    veo = MagicMock()
    _install_clients(monkeypatch, gemini=gemini, veo=veo)
    _mock_kenburns(monkeypatch, durations={})
    _mock_probe_ok(monkeypatch, durations={})

    result = runner.invoke(cli.app, [*_root(projects_root), "video_assets", SLUG])
    assert result.exit_code != 0

    record = _manifest(projects_root).stages["06_video_assets"]
    assert record.status == StageStatus.FAILED
    assert record.error is not None
    # GAP closure: the safety-block subtype is preserved as the error.type.
    assert record.error.type == "GeminiSafetyBlocked"
    assert "cost_usd" not in record.metrics
