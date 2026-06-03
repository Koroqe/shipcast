"""Full-pipeline MOCKED dry-run smoke test (Slice 22/23 — automatable portion).

Drives the REAL 11 pipeline stages end-to-end through the REAL CLI dispatcher
(`shipcast.cli` via Typer's `CliRunner`), in BOTH ``standard`` and ``premium``
video mode, with EVERY external client mocked away. This is the automatable heart
of the operator E2E smokes (Slices 22/23): it exercises the real stage code,
real manifest writes, the real human-gate approval flow, the real cost-cap gate,
and the real artifact wiring — catching integration gaps where one stage writes
an output the next stage cannot find. The real-API smokes (real keys, real spend,
the live `example-project`) are operator-driven and documented in
``docs/qa/shipcast_e2e_runbook.md``.

Design — REAL stages, mocked CLIENTS (vs Slice 21's fake cost stages)
--------------------------------------------------------------------
``test_cost_cap.py`` (Slice 21) drives the pipeline with FAKE cost-bearing
stages to assert the ledger arithmetic. This smoke instead drives the REAL stage
classes from the live ``ALL_STAGES`` registry and mocks only the EXTERNAL surface:

* Gemini ``multimodal`` (narrative) + ``generate_image`` (Imagen stills/cards).
* Veo ``generate_clip`` (premium hero clip).
* ElevenLabs ``synthesize_speech`` (narration mp3).
* WhisperX ``transcribe_with_alignment`` (word timestamps) + its on-PATH check.
* Every ``claude -p`` sub-agent (ba-analyst / planner / brand-guardian /
  demo-script-writer / social-copywriter / code-reviewer) via the stage modules'
  ``subprocess.run`` seam, plus the ``gh`` / ``git`` repo-signal calls.
* ffmpeg Ken-Burns / Veo clip rendering is replaced with a tiny REAL ``testsrc``
  1080x1920 clip (ffmpeg is installed on dev machines) so Stage 06 stays fast;
  Stage 08's concat/mix/caption/loop passes use REAL ffmpeg on those tiny clips.

The client-bundle stages (02/03/06/09) expose a ``_default_clients_factory``
module function; we monkeypatch each module's factory to return a bundle of
mocks. The sub-agent stages (02/04/05/10/11) and s02's repo-signal calls all go
through the SAME ``subprocess`` module object (each does ``import subprocess``),
so a SINGLE ``monkeypatch.setattr(subprocess, "run", …)`` replaces every shell-out
with a fake that dispatches on ``cmd[0]`` (gh/git/claude/ffmpeg) and, for
``claude``, on the ``--agent`` name. ffmpeg/ffprobe stay REAL. No real network /
API / ``claude`` is touched anywhere.

Asserted at the end (both modes):
* all 11 stages are ``done`` AND ``human_approved_at`` is set;
* ``11_package/release.zip`` is a readable archive containing the expected asset
  set (showcase + loop + 4 aspect cards + OG + 6 carousel slides + 3 copy md);
* ``11_package/README.md`` exists with the paste-blocks + asset table;
* accumulated cost is within the per-mode cap ($3 standard, $8 premium), and the
  premium run's Veo hero clip pushed it above the standard cap.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import shipcast.cli as cli
import shipcast.schemas as schemas
import shipcast.stages.s02_enrich as enrich_mod
import shipcast.stages.s03_brand as brand_mod
import shipcast.stages.s06_video_assets as video_assets_mod
import shipcast.stages.s07_voice as voice_mod
from shipcast.config import Settings
from shipcast.cost import VEO_FAST_CLIP_USD, accumulated_cost_usd
from shipcast.manifest import Manifest, StageStatus
from shipcast.marketing import hooks

runner = CliRunner()

# --------------------------------------------------------------------------- #
# Constants mirroring the canonical changelog fixture (tests/fixtures/repos/…).
# --------------------------------------------------------------------------- #

_REPO_FIXTURES = (
    Path(__file__).resolve().parent.parent / "fixtures" / "repos" / "example_min"
)
_CHANGELOG = (_REPO_FIXTURES / "CHANGELOG.md").read_text(encoding="utf-8")

ENTRY_HEADING = "Add CSV export"
BRAND_SLUG = "test-brand"
SLUG = "target-repo--add-csv-export"

#: The picked-entry mapping `hooks.render` sees — matches what s01_pick writes
#: from the canonical fixture (name + summary), so the rendered hook openings in
#: the copy bundle equal what s10_copy recomputes.
_ENTRY_FOR_HOOK: dict[str, Any] = {
    "name": ENTRY_HEADING,
    "summary": "Users can now download their report as a spreadsheet file.",
}

#: One hook key reused for all three copy channels (keeps the bundle simple).
_HOOK_KEY = "we_just_shipped"


def _make_real_png() -> bytes:
    """A small but FULLY-VALID PNG that PIL can open / convert / resize.

    s03_brand writes it as the logo and s09_graphics opens it as the Imagen
    background then resizes to each card's canonical dims, so it must be a real
    decodable image (a hand-crafted 1x1 stub trips PIL's chunk parser).
    """
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 64), (17, 34, 51)).save(buf, format="PNG")
    return buf.getvalue()


#: A small real PNG used as the mocked Gemini Imagen output + brand logo bytes.
_REAL_PNG = _make_real_png()

#: Expected ZIP asset set (the always-present subset; this scenario sets
#: has_stat_card=False and has_code_screenshot=False so no stat_*/code.png).
_EXPECTED_ZIP_MEMBERS = {
    "08_video/showcase.mp4",
    "08_video/loop_6s.mp4",
    "08_video/loop_6s.gif",
    "09_graphics/1x1.png",
    "09_graphics/16x9.png",
    "09_graphics/9x16.png",
    "09_graphics/4x5.png",
    "09_graphics/og_card.png",
    "09_graphics/carousel/slide_01.png",
    "09_graphics/carousel/slide_02.png",
    "09_graphics/carousel/slide_03.png",
    "09_graphics/carousel/slide_04.png",
    "09_graphics/carousel/slide_05.png",
    "09_graphics/carousel/slide_06.png",
    "10_copy/twitter_thread.md",
    "10_copy/linkedin.md",
    "10_copy/blog.md",
}

#: Dispatch order: each stage runs only after its upstreams are done+approved.
_DISPATCH_ORDER: tuple[tuple[str, str], ...] = (
    ("pick", "01_pick"),
    ("enrich", "02_enrich"),
    ("brand", "03_brand"),
    ("plan", "04_plan"),
    ("script", "05_script"),
    ("video_assets", "06_video_assets"),
    ("voice", "07_voice"),
    ("video", "08_video"),
    ("graphics", "09_graphics"),
    ("copy", "10_copy"),
    ("package", "11_package"),
)


# --------------------------------------------------------------------------- #
# Mock-payload builders (valid against the real stage schemas).
# --------------------------------------------------------------------------- #


def _valid_brief() -> dict[str, Any]:
    """A schema-valid MarketingBrief: 4 video beats, 4 carousel beats, all hooks
    set to ``_HOOK_KEY`` so the copy bundle's openings can match deterministically.
    has_stat_card / has_code_screenshot are False to keep the asset set fixed.
    """
    return {
        "hook_template_per_channel": {
            "x": _HOOK_KEY,
            "linkedin": _HOOK_KEY,
            "blog": _HOOK_KEY,
        },
        "ctas": ["Try it now", "Read the docs"],
        "video_beats": [
            {
                "image_prompt": f"beat {i} visual",
                "narration": f"Beat {i} narration line.",
                "duration_sec": 4.0,
            }
            for i in range(4)
        ],
        "carousel_beats": [
            {"headline": f"slide {i}", "body": f"body {i}"} for i in range(4)
        ],
        "has_stat_card": False,
        "has_code_screenshot": False,
    }


def _valid_storyboard() -> dict[str, Any]:
    """A schema-valid Storyboard: exactly 4 beats, each 3-5 s on screen."""
    return {
        "beats": [
            {
                "image_prompt": f"showcase beat {i}",
                "narration": f"Showcase narration beat {i}.",
                "duration_sec": 4.0,
            }
            for i in range(4)
        ]
    }


def _valid_copy_bundle() -> dict[str, str]:
    """A schema-valid CopyBundle whose channels open with the rendered hook."""
    hook = hooks.render(_HOOK_KEY, _ENTRY_FOR_HOOK)

    twitter = "\n".join(
        [f"1/ {hook} Here is the thread."]
        + [f"{i}/ One idea per tweet, point {i}." for i in range(2, 5)]
    )

    linkedin_words = ["value"] * (700 - len(hook.split()) - 12)
    linkedin = (
        f"{hook}\n\n"
        + " ".join(linkedin_words)
        + "\n\n→ one click downloads your whole report.\n"
        + "▸ streams large datasets without timing out.\n\n"
        + "What would you automate with it?\n\n"
        + "#ship #build #devtools"
    )

    blog_words = ["word"] * (1300 - len(hook.split()) - 8)
    blog = f"{hook}\n\nTL;DR: it ships.\n\n" + " ".join(blog_words) + "\n\nThe end."

    return {"twitter_thread": twitter, "linkedin": linkedin, "blog": blog}


# --------------------------------------------------------------------------- #
# ffmpeg seam — tiny REAL testsrc clip generators (fast; ultrafast preset).
# --------------------------------------------------------------------------- #


def _make_testsrc_clip(path: Path, *, seconds: float) -> Path:
    """Write a tiny 1080x1920 h264 clip via ffmpeg testsrc (ultrafast, no audio)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"testsrc=size=1080x1920:rate=30:duration={seconds}",
            "-t", f"{seconds:.3f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p", "-an",
            "-f", "mp4", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _make_sine_mp3(path: Path, *, seconds: float) -> Path:
    """Write a tiny narration mp3 via ffmpeg sine generator."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
            "-c:a", "libmp3lame", "-b:a", "96k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


# --------------------------------------------------------------------------- #
# Stage-by-stage mock installers.
# --------------------------------------------------------------------------- #


def _bundle(**attrs: Any) -> Any:
    """Build a duck-typed clients bundle object from keyword attributes."""

    class _B:
        pass

    b = _B()
    for k, v in attrs.items():
        setattr(b, k, v)
    return b


def _install_subprocess_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install ONE `subprocess.run` fake covering every stage's shell-outs.

    All the sub-agent stages (02/04/05/10/11) and s02's repo-signal calls share
    the SAME ``subprocess`` module object (each does ``import subprocess``), so a
    single ``monkeypatch.setattr(subprocess, "run", …)`` is the only correct seam
    — per-module patches would clobber one another (last-installer-wins). The
    fake dispatches on ``cmd[0]`` and, for ``claude -p``, on the ``--agent`` name,
    returning the canned JSON each sub-agent's parser expects.
    """
    real_run = subprocess.run
    brief_json = json.dumps(_valid_brief())
    storyboard_json = json.dumps(_valid_storyboard())
    cb = _valid_copy_bundle()
    copy_markers = (
        f"<<<TWITTER>>>\n{cb['twitter_thread']}\n"
        f"<<<LINKEDIN>>>\n{cb['linkedin']}\n"
        f"<<<BLOG>>>\n{cb['blog']}\n<<<END>>>\n"
    )

    def _fake_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        head = cmd[0]
        if head in ("gh", "git"):
            # s02 repo signals — degrade gracefully (empty stdout).
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if head == "claude":
            # brand-guardian is the only `--agent` call; the rest are plain
            # `claude -p` dispatched by prompt content (the last argv item).
            if "--agent" in cmd:
                stdout = brief_json  # brand-guardian guards the brief
            else:
                prompt = cmd[-1]
                if "marketing framing" in prompt:
                    stdout = '{"framing": "ok"}'
                elif "showcase storyboard" in prompt:
                    stdout = storyboard_json
                elif "marketing brief" in prompt:
                    stdout = brief_json  # planner draft
                elif "<<<TWITTER>>>" in prompt:
                    stdout = copy_markers  # social-copywriter
                elif "README" in prompt:
                    stdout = "LGTM — no broken links."  # code-reviewer
                else:  # pragma: no cover - guard against an unmapped prompt
                    raise AssertionError(f"unmapped claude prompt: {prompt[:80]!r}")
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        # ffmpeg / ffprobe stay REAL (testsrc clips are genuine 1080x1920 h264).
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", _fake_run)


def _install_enrich_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock s02_enrich's Gemini multimodal narrative (subprocess via shared fake)."""
    from unittest.mock import MagicMock

    gemini = MagicMock()
    gemini.multimodal.return_value = "A compelling, on-brand marketing narrative."

    monkeypatch.setattr(
        enrich_mod,
        "_default_clients_factory",
        lambda project: _bundle(gemini=gemini, playwright=None),
    )


def _install_brand_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock s03_brand: Gemini style-sheet image only (pack ships png logo + hint)."""
    from unittest.mock import MagicMock

    gemini = MagicMock()
    gemini.generate_image.return_value = _REAL_PNG

    monkeypatch.setattr(
        brand_mod,
        "_default_clients_factory",
        lambda project: _bundle(gemini=gemini, playwright=MagicMock()),
    )


def _install_video_assets_mocks(monkeypatch: pytest.MonkeyPatch, *, premium: bool) -> None:
    """Mock s06_video_assets.

    * Imagen ``generate_image`` returns a real PNG; the Ken-Burns render is
      replaced with a tiny real testsrc clip so the stage stays fast.
    * In premium mode the Veo hero clip is written by a tiny real testsrc clip
      (8 s) so the cost path + duration are exercised without spending.
    * ``probe_video`` stays REAL (the testsrc clips genuinely are 1080x1920 h264).
    """
    from unittest.mock import MagicMock

    gemini = MagicMock()
    gemini.generate_image.return_value = _REAL_PNG

    veo = MagicMock()

    def _veo_generate_clip(
        prompt: str,
        *,
        model: str,
        output_path: Path,
        conditioning_image: Path | None = None,
    ) -> Path:
        return _make_testsrc_clip(output_path, seconds=8.0)

    veo.generate_clip.side_effect = _veo_generate_clip

    monkeypatch.setattr(
        video_assets_mod,
        "_default_clients_factory",
        lambda project: _bundle(gemini=gemini, veo=veo),
    )

    # Replace the (slow, medium-preset) Ken-Burns render with a tiny testsrc clip.
    def _fake_ken_burns(
        *, still_path: Path, duration_sec: float, output_path: Path, fast: bool = False
    ) -> Path:
        return _make_testsrc_clip(output_path, seconds=float(duration_sec))

    monkeypatch.setattr(
        video_assets_mod._ffmpeg, "ken_burns_clip", _fake_ken_burns
    )


def _install_voice_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock s07_voice: whisper-backend gate, ElevenLabs synth, WhisperX align."""
    from unittest.mock import MagicMock

    from shipcast.schemas import WordTimestamp

    # The check_inputs gate verifies the openai-whisper backend is importable;
    # force it "present" so the mocked alignment client is used (no real model).
    monkeypatch.setattr(voice_mod, "_whisper_installed", lambda: True)

    elevenlabs = MagicMock()

    def _synth(
        text: str,
        voice_id: str,
        output_path: Path,
        *,
        model: str,
        voice_settings: dict[str, Any] | None = None,
    ) -> Path:
        return _make_sine_mp3(output_path, seconds=18.0)

    elevenlabs.synthesize_speech.side_effect = _synth

    whisperx = MagicMock()
    whisperx.transcribe_with_alignment.return_value = [
        WordTimestamp(word="Ship", start_sec=0.0, end_sec=0.4),
        WordTimestamp(word="faster", start_sec=0.5, end_sec=0.9),
        WordTimestamp(word="today", start_sec=1.0, end_sec=1.4),
    ]

    monkeypatch.setattr(
        voice_mod,
        "_default_clients_factory",
        lambda project: _bundle(elevenlabs=elevenlabs, whisperx=whisperx),
    )


def _install_graphics_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock s09_graphics: Gemini Imagen card backgrounds (carousel/code are pure)."""
    from unittest.mock import MagicMock

    gemini = MagicMock()
    gemini.generate_image.return_value = _REAL_PNG

    import shipcast.stages.s09_graphics as graphics_mod

    monkeypatch.setattr(
        graphics_mod,
        "_default_clients_factory",
        lambda project: _bundle(gemini=gemini),
    )


# --------------------------------------------------------------------------- #
# Project / brand-pack scaffolding.
# --------------------------------------------------------------------------- #


def _seed_brand_pack(projects_root: Path) -> None:
    """Materialize a complete brand pack with a palette hint (skips Playwright)."""
    root = projects_root / "_brand" / BRAND_SLUG
    (root / "fonts").mkdir(parents=True, exist_ok=True)
    (root / "voice.md").write_text("# Voice\ncaption_mode: chip\n", encoding="utf-8")
    (root / "fonts" / "Inter.ttf").write_bytes(b"TTF-BYTES")
    (root / "logo.png").write_bytes(_REAL_PNG)
    (root / "palette.hint.json").write_text(
        json.dumps({"primary": "#112233", "accent": "#445566", "neutral": "#778899"}),
        encoding="utf-8",
    )


def _make_target_repo(repo_root: Path) -> Path:
    """A tmp target repo with the canonical CHANGELOG fixture under the allowed root."""
    repo = repo_root / "target-repo"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")
    return repo


def _root(projects_root: Path) -> list[str]:
    return ["--projects-root", str(projects_root)]


def _set_settings_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Force every `Settings.from_files` to report the requested video_mode.

    The cost CAP derives from `settings.video_mode` (config.toml `default_mode`),
    NOT from input.yaml. So a premium run needs the settings mode to be premium
    too, or the $8 cap would not apply and the Veo $3.20 gate would abort. The
    per-project RENDER mode (whether Veo is used) is read separately from
    input.yaml by s06; both must agree for the premium path.
    """
    real_from_files = Settings.from_files.__func__  # type: ignore[attr-defined]

    def _from_files(cls: Any, *a: Any, **k: Any) -> Settings:
        settings = real_from_files(cls, *a, **k)
        return settings.model_copy(update={"video_mode": mode})

    monkeypatch.setattr(Settings, "from_files", classmethod(_from_files))


def _approve(projects_root: Path, stage_id: str) -> None:
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, stage_id])
    assert result.exit_code == 0, f"approve {stage_id} failed: {result.output}"


# --------------------------------------------------------------------------- #
# The full-pipeline driver.
# --------------------------------------------------------------------------- #


def _run_full_pipeline(
    projects_root: Path,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    video_mode: str,
) -> Manifest:
    """Drive pick→…→package through the real dispatcher, approving between each."""
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", repo_root)
    _set_settings_mode(monkeypatch, video_mode)
    target_repo = _make_target_repo(repo_root)
    _seed_brand_pack(projects_root)

    premium = video_mode == "premium"

    # Install every external mock up front (inert until the owning stage runs).
    # ONE subprocess fake covers all sub-agent + gh/git shell-outs; the
    # client-bundle stages each get their factory replaced with a mock bundle.
    _install_subprocess_fake(monkeypatch)
    _install_enrich_mocks(monkeypatch)
    _install_brand_mocks(monkeypatch)
    _install_video_assets_mocks(monkeypatch, premium=premium)
    _install_voice_mocks(monkeypatch)
    _install_graphics_mocks(monkeypatch)

    # pick (create mode): bootstraps the project from the repo path + heading.
    result = runner.invoke(
        cli.app,
        [*_root(projects_root), "pick", str(target_repo), "--entry", ENTRY_HEADING],
    )
    assert result.exit_code == 0, result.output

    # Overwrite input.yaml so brand_slug + video_mode are exactly what we want
    # (pick derives brand_slug from the repo name; the smoke pins it).
    input_path = projects_root / SLUG / "input.yaml"
    input_path.write_text(
        yaml.safe_dump(
            {
                "repo_path": str(target_repo),
                "entry_heading": ENTRY_HEADING,
                "brand_slug": BRAND_SLUG,
                "video_mode": video_mode,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _approve(projects_root, "01_pick")

    # Every remaining stage: dispatch then approve.
    for verb, stage_id in _DISPATCH_ORDER[1:]:
        result = runner.invoke(cli.app, [*_root(projects_root), verb, SLUG])
        assert result.exit_code == 0, f"{verb} failed: {result.output}"
        _approve(projects_root, stage_id)

    return Manifest.load(projects_root / SLUG / "manifest.json")


def _assert_pipeline_complete(manifest: Manifest) -> None:
    """Every stage reached done + approved."""
    for stage_id, record in manifest.stages.items():
        assert record.status == StageStatus.DONE, (
            f"stage {stage_id} status is {record.status.value}, expected done"
        )
        assert record.human_approved_at is not None, (
            f"stage {stage_id} is not human-approved"
        )


def _assert_release_package(projects_root: Path) -> None:
    """release.zip is a readable archive with the expected asset set; README exists."""
    pkg_dir = projects_root / SLUG / "11_package"
    zip_path = pkg_dir / "release.zip"
    readme_path = pkg_dir / "README.md"

    assert zip_path.is_file(), "release.zip missing"
    assert readme_path.is_file(), "README.md missing"
    assert zipfile.is_zipfile(zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        members = set(zf.namelist())
    assert members == _EXPECTED_ZIP_MEMBERS, (
        f"unexpected ZIP members.\n  missing: {_EXPECTED_ZIP_MEMBERS - members}\n"
        f"  extra:   {members - _EXPECTED_ZIP_MEMBERS}"
    )

    readme = readme_path.read_text(encoding="utf-8")
    assert "## Paste-ready copy" in readme
    assert "## Assets" in readme
    assert "| Asset | Dimensions | Aspect |" in readme
    # One fenced block per copy channel (>= 3).
    assert readme.count("```text") >= 3


# --------------------------------------------------------------------------- #
# Tests — both modes.
# --------------------------------------------------------------------------- #


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos_root"
    root.mkdir()
    return root


def test_full_pipeline_smoke_standard_mode(
    projects_root: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standard mode: all 11 real stages run end-to-end, fully mocked, ≤ $3."""
    manifest = _run_full_pipeline(
        projects_root, repo_root, monkeypatch, video_mode="standard"
    )

    _assert_pipeline_complete(manifest)
    _assert_release_package(projects_root)

    total = accumulated_cost_usd(manifest)
    assert total <= 3.0, f"standard total ${total:.2f} exceeds the $3 cap"
    # Standard mode never touches Veo.
    assert total < VEO_FAST_CLIP_USD, total


def test_full_pipeline_smoke_premium_mode(
    projects_root: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Premium mode: same pipeline with the Veo hero clip, fully mocked, ≤ $8."""
    manifest = _run_full_pipeline(
        projects_root, repo_root, monkeypatch, video_mode="premium"
    )

    _assert_pipeline_complete(manifest)
    _assert_release_package(projects_root)

    # The premium hero beat[0] was rendered as an ~8 s clip via the Veo seam.
    clips_json = json.loads(
        (projects_root / SLUG / "06_video_assets" / "clips.json").read_text(
            encoding="utf-8"
        )
    )
    assert clips_json["mode"] == "premium"
    assert clips_json["clips"][0]["source"] == "veo"
    assert abs(clips_json["clips"][0]["duration_sec"] - 8.0) < 0.5

    total = accumulated_cost_usd(manifest)
    assert total <= 8.0, f"premium total ${total:.2f} exceeds the $8 cap"
    # Premium genuinely needs its raised cap: the Veo hero pushes it over $3.
    assert total > 3.0, total
