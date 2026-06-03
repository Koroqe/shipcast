"""Integration tests for the `s05_script` stage + the `script` CLI verb (Slice 12).

Owned TCs (Section 8):
- TC-8.1: happy path — mocked `demo-script-writer` returns a 4-beat Storyboard →
          schema-valid `05_script/storyboard.json`; 4 beats each with
          image_prompt / narration / duration_sec.
- TC-8.2: 6 beats (max boundary) accepted → storyboard.json has 6 beats.
- TC-8.3: 3 beats (below min) → SubagentMalformedOutput, FAILED, no storyboard.
- TC-8.4: 7 beats (above max) → SubagentMalformedOutput, FAILED, no storyboard.
- TC-8.5: sub-agent TimeoutExpired → SubagentTimeout, FAILED, no storyboard.
- TC-8.6: a beat missing `narration` → validate fails, FAILED, no storyboard.

The single `claude -p` call is mocked through the stage's `subprocess.run`. No
real `claude` / network / browser. The project is driven to "04_plan
done+approved" via the runner with mocked enrich + brand + plan sub-agents.
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
from shipcast.errors import SubagentMalformedOutput, SubagentTimeout
from shipcast.manifest import Manifest, StageStatus
from shipcast.schemas import Storyboard

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


# --------------------------------------------------------------------------- #
# Storyboard JSON fixtures (pinned mock sub-agent stdout)
# --------------------------------------------------------------------------- #


def _valid_storyboard(
    *,
    n_beats: int = 4,
    drop_narration: bool = False,
    duration_sec: float = 4.0,
) -> dict[str, Any]:
    """A Storyboard-shaped dict (configurable for the negative cases)."""
    beats: list[dict[str, Any]] = []
    for i in range(n_beats):
        beat: dict[str, Any] = {
            "image_prompt": f"beat {i} visual",
            "narration": f"beat {i} narration",
            "duration_sec": duration_sec,
        }
        if drop_narration:
            del beat["narration"]
        beats.append(beat)
    return {"beats": beats}


def _valid_brief() -> dict[str, Any]:
    """A MarketingBrief-shaped dict to feed the mocked plan sub-agents."""
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


def _drive_to_plan_approved(
    projects_root: Path,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run pick → enrich → brand → plan and approve each, so s05_script's gate passes."""
    _seed_brand_pack(projects_root)

    # pick (create mode)
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

    # enrich (mocked gemini + no-op gh/git/claude)
    gemini = MagicMock()
    gemini.multimodal.return_value = "A compelling marketing narrative."

    def _enrich_factory(project: Any) -> Any:
        class _B:
            def __init__(self) -> None:
                self.gemini = gemini
                self.playwright = None

        return _B()

    monkeypatch.setattr(enrich_mod, "_default_clients_factory", _enrich_factory)

    def _fake_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        if cmd[0] in ("gh", "git"):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "claude":
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd!r}")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "02_enrich"])
    assert result.exit_code == 0, result.output

    # brand (mocked gemini image only; pack ships png logo + no live_url)
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

    # plan (mocked chained planner → brand-guardian, both returning a valid brief)
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


def _install_script_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str,
    timeout: bool = False,
    returncode: int = 0,
    stderr: str = "",
) -> MagicMock:
    """Patch the script stage's `subprocess.run` to fake the demo-script-writer call."""
    calls = MagicMock()

    def _fake_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        calls(cmd)
        assert cmd[0] == "claude", f"unexpected subprocess: {cmd!r}"
        # Plain `claude -p` (no --agent) — the script call uses the default agent.
        assert "--agent" not in cmd
        if timeout:
            raise subprocess.TimeoutExpired(cmd, timeout=300)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(script_mod.subprocess, "run", _fake_run)
    return calls


def _storyboard_path(projects_root: Path) -> Path:
    return projects_root / SLUG / "05_script" / "storyboard.json"


# --------------------------------------------------------------------------- #
# TC-8.1 — happy path (4 beats)
# --------------------------------------------------------------------------- #


def test_tc_8_1_happy_path_four_beats(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-8.1: mocked demo-script-writer → schema-valid 4-beat storyboard.json."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    calls = _install_script_subprocess(
        monkeypatch, stdout=json.dumps(_valid_storyboard(n_beats=4))
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "script", SLUG])
    assert result.exit_code == 0, result.output

    path = _storyboard_path(projects_root)
    assert path.is_file()
    storyboard = Storyboard.model_validate_json(path.read_text(encoding="utf-8"))
    assert len(storyboard.beats) == 4
    for beat in storyboard.beats:
        assert beat.image_prompt
        assert beat.narration
        assert 3.0 <= beat.duration_sec <= 5.0

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["05_script"].status == StageStatus.DONE
    assert "05_script/storyboard.json" in m.stages["05_script"].outputs

    # Exactly one storyboard call — plain `claude -p` (default agent, no --agent).
    assert calls.call_count == 1
    assert "--agent" not in calls.call_args_list[0].args[0]


# --------------------------------------------------------------------------- #
# TC-8.2 — six beats (max boundary) accepted
# --------------------------------------------------------------------------- #


def test_tc_8_2_six_beats_accepted(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-8.2: 6 beats (max boundary) → storyboard.json has 6 beats."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_script_subprocess(
        monkeypatch, stdout=json.dumps(_valid_storyboard(n_beats=6))
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "script", SLUG])
    assert result.exit_code == 0, result.output

    storyboard = Storyboard.model_validate_json(
        _storyboard_path(projects_root).read_text(encoding="utf-8")
    )
    assert len(storyboard.beats) == 6


# --------------------------------------------------------------------------- #
# TC-8.3 — three beats (below min) → SubagentMalformedOutput
# --------------------------------------------------------------------------- #


def test_tc_8_3_three_beats_malformed(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-8.3: 3 beats → SubagentMalformedOutput, FAILED, no storyboard.json."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_script_subprocess(
        monkeypatch, stdout=json.dumps(_valid_storyboard(n_beats=3))
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "script", SLUG])
    assert result.exit_code != 0, result.output

    assert not _storyboard_path(projects_root).exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["05_script"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentMalformedOutput"


# --------------------------------------------------------------------------- #
# TC-8.4 — seven beats (above max) → SubagentMalformedOutput
# --------------------------------------------------------------------------- #


def test_tc_8_4_seven_beats_malformed(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-8.4: 7 beats → SubagentMalformedOutput, FAILED, no storyboard.json."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_script_subprocess(
        monkeypatch, stdout=json.dumps(_valid_storyboard(n_beats=7))
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "script", SLUG])
    assert result.exit_code != 0, result.output

    assert not _storyboard_path(projects_root).exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["05_script"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentMalformedOutput"


# --------------------------------------------------------------------------- #
# TC-8.5 — sub-agent timeout → SubagentTimeout
# --------------------------------------------------------------------------- #


def test_tc_8_5_timeout(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-8.5: TimeoutExpired → SubagentTimeout, FAILED, no storyboard.json."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_script_subprocess(monkeypatch, stdout="", timeout=True)

    result = runner.invoke(cli.app, [*_root(projects_root), "script", SLUG])
    assert result.exit_code != 0, result.output

    assert not _storyboard_path(projects_root).exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["05_script"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentTimeout"


# --------------------------------------------------------------------------- #
# TC-8.6 — beat missing `narration` → validation failure
# --------------------------------------------------------------------------- #


def test_tc_8_6_missing_narration_field(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-8.6: a beat without `narration` → FAILED, no storyboard.json."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_script_subprocess(
        monkeypatch,
        stdout=json.dumps(_valid_storyboard(n_beats=4, drop_narration=True)),
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "script", SLUG])
    assert result.exit_code != 0, result.output

    assert not _storyboard_path(projects_root).exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["05_script"].status == StageStatus.FAILED


# --------------------------------------------------------------------------- #
# Out-of-window duration → validation failure (3-5 s rule)
# --------------------------------------------------------------------------- #


def test_duration_out_of_window_fails(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A beat with duration_sec outside [3, 5] → FAILED, no storyboard.json."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_script_subprocess(
        monkeypatch,
        stdout=json.dumps(_valid_storyboard(n_beats=4, duration_sec=6.0)),
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "script", SLUG])
    assert result.exit_code != 0, result.output

    assert not _storyboard_path(projects_root).exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["05_script"].status == StageStatus.FAILED


# --------------------------------------------------------------------------- #
# Direct-unit error-path coverage (no full runner)
# --------------------------------------------------------------------------- #


def test_invoke_subagent_timeout_direct() -> None:
    """`_invoke_subagent` raises SubagentTimeout on TimeoutExpired."""

    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, timeout=300)

    stage = script_mod.ScriptStage(subprocess_run=_run)
    with pytest.raises(SubagentTimeout):
        stage._invoke_subagent("demo-script-writer", "prompt")


def test_invoke_subagent_non_object_json_direct() -> None:
    """`_invoke_subagent` raises SubagentMalformedOutput when stdout is a JSON list."""

    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        return subprocess.CompletedProcess(cmd, 0, stdout="[1, 2, 3]", stderr="")

    stage = script_mod.ScriptStage(subprocess_run=_run)
    with pytest.raises(SubagentMalformedOutput):
        stage._invoke_subagent("demo-script-writer", "prompt")
