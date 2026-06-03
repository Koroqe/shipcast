"""Integration tests for the `s04_plan` stage + the `plan` CLI verb (Slice 11).

Owned TCs (Section 7 + Section 20):
- TC-7.1:  happy path — chained planner + brand-guardian, both `claude -p` calls
           mocked → schema-valid `04_plan/brief.json`; video_beats==4,
           carousel_beats==4, every hook value in the 7-key catalog, ctas
           non-empty, has_stat_card / has_code_screenshot booleans.
- TC-7.3:  brand-guardian's values OVERRIDE the planner draft (guardian wins).
- TC-7.4:  planner subprocess TimeoutExpired → SubagentTimeout, FAILED, no brief.
- TC-7.5:  planner succeeds, guardian TimeoutExpired → SubagentTimeout, FAILED.
- TC-7.6:  either agent returns non-JSON stdout → SubagentMalformedOutput, FAILED.
- TC-7.7:  guardian returns video_beats of length 3 → validate fails, FAILED,
           no brief.json written.
- TC-20.2: removing `03_brand/voice.md` makes `s04_plan.check_inputs` raise
           StageInputMissing.

The two chained `claude -p` calls are mocked through the stage's injected
`subprocess.run`. No real `claude` / network / browser. The project is driven to
"03_brand done+approved" via the runner with mocked enrich + brand clients.
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
from shipcast.errors import (
    SubagentMalformedOutput,
    SubagentTimeout,
)
from shipcast.manifest import Manifest, StageStatus
from shipcast.schemas import MarketingBrief

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
# Brief JSON fixtures (pinned mock sub-agent stdout)
# --------------------------------------------------------------------------- #


def _valid_brief(
    *,
    hook_x: str = "we_just_shipped",
    hook_linkedin: str = "before_after",
    hook_blog: str = "problem_aha",
    n_video_beats: int = 4,
    has_stat: bool = True,
    has_code: bool = False,
) -> dict[str, Any]:
    """A MarketingBrief-shaped dict (configurable for the negative cases)."""
    return {
        "hook_template_per_channel": {
            "x": hook_x,
            "linkedin": hook_linkedin,
            "blog": hook_blog,
        },
        "ctas": ["Try it now", "Read the docs"],
        "video_beats": [
            {
                "image_prompt": f"beat {i} visual",
                "narration": f"beat {i} line",
                "duration_sec": 4.0,
            }
            for i in range(n_video_beats)
        ],
        "carousel_beats": [
            {"headline": f"slide {i}", "body": f"body {i}"} for i in range(4)
        ],
        "has_stat_card": has_stat,
        "has_code_screenshot": has_code,
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
    # palette.hint.json lets s03_brand skip Playwright palette extraction entirely
    # (no live_url needed), keeping the s04_plan setup self-contained.
    (root / "palette.hint.json").write_text(
        json.dumps({"primary": "#112233", "accent": "#445566", "neutral": "#778899"}),
        encoding="utf-8",
    )


def _drive_to_brand_approved(
    projects_root: Path,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run pick → enrich → brand and approve each, so s04_plan's gate is satisfied."""
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


def _install_plan_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    planner_stdout: str,
    guardian_stdout: str,
    planner_timeout: bool = False,
    guardian_timeout: bool = False,
    planner_returncode: int = 0,
    guardian_returncode: int = 0,
    planner_stderr: str = "",
    guardian_stderr: str = "",
) -> MagicMock:
    """Patch the plan stage's `subprocess.run` to fake the two chained claude calls.

    The first `claude --agent planner` call returns `planner_stdout`; the second
    `claude --agent brand-guardian` call returns `guardian_stdout`. Returns the
    mock so call order / args can be asserted.
    """
    calls = MagicMock()

    def _fake_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        calls(cmd)
        assert cmd[0] == "claude", f"unexpected subprocess: {cmd!r}"
        agent = cmd[cmd.index("--agent") + 1]
        if agent == "planner":
            if planner_timeout:
                raise subprocess.TimeoutExpired(cmd, timeout=300)
            return subprocess.CompletedProcess(
                cmd, planner_returncode, stdout=planner_stdout, stderr=planner_stderr
            )
        if agent == "brand-guardian":
            if guardian_timeout:
                raise subprocess.TimeoutExpired(cmd, timeout=300)
            return subprocess.CompletedProcess(
                cmd, guardian_returncode, stdout=guardian_stdout, stderr=guardian_stderr
            )
        raise AssertionError(f"unexpected agent: {agent!r}")

    monkeypatch.setattr(plan_mod.subprocess, "run", _fake_run)
    return calls


# --------------------------------------------------------------------------- #
# TC-7.1 — happy path
# --------------------------------------------------------------------------- #


def test_tc_7_1_happy_path(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-7.1: chained planner + guardian → schema-valid brief with hard lengths."""
    _drive_to_brand_approved(projects_root, target_repo, monkeypatch)
    calls = _install_plan_subprocess(
        monkeypatch,
        planner_stdout=json.dumps(_valid_brief()),
        guardian_stdout=json.dumps(_valid_brief()),
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "plan", SLUG])
    assert result.exit_code == 0, result.output

    brief_path = projects_root / SLUG / "04_plan" / "brief.json"
    assert brief_path.is_file()
    brief = MarketingBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
    assert len(brief.video_beats) == 4
    assert len(brief.carousel_beats) == 4
    from shipcast.marketing import hooks

    for value in brief.hook_template_per_channel.values():
        assert value in hooks.KEYS
    assert brief.ctas
    assert isinstance(brief.has_stat_card, bool)
    assert isinstance(brief.has_code_screenshot, bool)

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["04_plan"].status == StageStatus.DONE
    assert "04_plan/brief.json" in m.stages["04_plan"].outputs

    # Chained, sequential: planner first, guardian second (exactly two calls).
    assert calls.call_count == 2
    first_cmd = calls.call_args_list[0].args[0]
    second_cmd = calls.call_args_list[1].args[0]
    assert "planner" in first_cmd
    assert "brand-guardian" in second_cmd


# --------------------------------------------------------------------------- #
# TC-7.3 — guardian overrides planner
# --------------------------------------------------------------------------- #


def test_tc_7_3_guardian_overrides_planner(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-7.3: the persisted brief reflects the guardian's values, not the draft."""
    _drive_to_brand_approved(projects_root, target_repo, monkeypatch)
    planner_draft = _valid_brief(hook_x="we_just_shipped")
    guardian_final = _valid_brief(hook_x="social_proof")  # guardian changed the hook
    _install_plan_subprocess(
        monkeypatch,
        planner_stdout=json.dumps(planner_draft),
        guardian_stdout=json.dumps(guardian_final),
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "plan", SLUG])
    assert result.exit_code == 0, result.output

    brief_path = projects_root / SLUG / "04_plan" / "brief.json"
    brief = MarketingBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
    assert brief.hook_template_per_channel["x"] == "social_proof"  # guardian's value


# --------------------------------------------------------------------------- #
# TC-7.4 — planner timeout
# --------------------------------------------------------------------------- #


def test_tc_7_4_planner_timeout(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-7.4: planner TimeoutExpired → SubagentTimeout, FAILED, no brief."""
    _drive_to_brand_approved(projects_root, target_repo, monkeypatch)
    _install_plan_subprocess(
        monkeypatch,
        planner_stdout="",
        guardian_stdout="",
        planner_timeout=True,
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "plan", SLUG])
    assert result.exit_code != 0, result.output

    assert not (projects_root / SLUG / "04_plan" / "brief.json").exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["04_plan"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentTimeout"


# --------------------------------------------------------------------------- #
# TC-7.5 — guardian timeout
# --------------------------------------------------------------------------- #


def test_tc_7_5_guardian_timeout(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-7.5: planner ok, guardian TimeoutExpired → SubagentTimeout, FAILED."""
    _drive_to_brand_approved(projects_root, target_repo, monkeypatch)
    _install_plan_subprocess(
        monkeypatch,
        planner_stdout=json.dumps(_valid_brief()),
        guardian_stdout="",
        guardian_timeout=True,
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "plan", SLUG])
    assert result.exit_code != 0, result.output

    assert not (projects_root / SLUG / "04_plan" / "brief.json").exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["04_plan"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentTimeout"


# --------------------------------------------------------------------------- #
# TC-7.6 — malformed JSON (planner)
# --------------------------------------------------------------------------- #


def test_tc_7_6_planner_malformed_json(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-7.6: planner returns non-JSON stdout → SubagentMalformedOutput, FAILED."""
    _drive_to_brand_approved(projects_root, target_repo, monkeypatch)
    _install_plan_subprocess(
        monkeypatch,
        planner_stdout="not json",
        guardian_stdout=json.dumps(_valid_brief()),
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "plan", SLUG])
    assert result.exit_code != 0, result.output

    assert not (projects_root / SLUG / "04_plan" / "brief.json").exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["04_plan"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentMalformedOutput"


def test_tc_7_6_guardian_malformed_json(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-7.6 (guardian variant): guardian returns non-JSON → SubagentMalformedOutput."""
    _drive_to_brand_approved(projects_root, target_repo, monkeypatch)
    _install_plan_subprocess(
        monkeypatch,
        planner_stdout=json.dumps(_valid_brief()),
        guardian_stdout="<<<not json>>>",
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "plan", SLUG])
    assert result.exit_code != 0, result.output

    assert not (projects_root / SLUG / "04_plan" / "brief.json").exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["04_plan"].status == StageStatus.FAILED


# --------------------------------------------------------------------------- #
# TC-7.7 — schema validation failure (video_beats length 3)
# --------------------------------------------------------------------------- #


def test_tc_7_7_video_beats_length_violation(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-7.7: guardian returns video_beats of length 3 → FAILED, no brief.json."""
    _drive_to_brand_approved(projects_root, target_repo, monkeypatch)
    _install_plan_subprocess(
        monkeypatch,
        planner_stdout=json.dumps(_valid_brief()),
        guardian_stdout=json.dumps(_valid_brief(n_video_beats=3)),
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "plan", SLUG])
    assert result.exit_code != 0, result.output

    assert not (projects_root / SLUG / "04_plan" / "brief.json").exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["04_plan"].status == StageStatus.FAILED


# --------------------------------------------------------------------------- #
# TC-20.2 — missing 03_brand/voice.md makes the gate raise
# --------------------------------------------------------------------------- #


def test_tc_20_2_missing_voice_md_blocks_check_inputs(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-20.2: deleting 03_brand/voice.md → s04_plan.check_inputs raises StageInputMissing."""
    from shipcast.errors import StageInputMissing
    from shipcast.project import Project

    _drive_to_brand_approved(projects_root, target_repo, monkeypatch)
    voice = projects_root / SLUG / "03_brand" / "voice.md"
    assert voice.is_file()
    voice.unlink()

    project = Project.load(projects_root, SLUG)
    with pytest.raises(StageInputMissing):
        plan_mod.PlanStage().check_inputs(project)


# --------------------------------------------------------------------------- #
# Direct-unit error-path coverage (no full runner) — exercises the helper
# --------------------------------------------------------------------------- #


def test_invoke_subagent_timeout_direct() -> None:
    """`_invoke_subagent` raises SubagentTimeout on TimeoutExpired."""

    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, timeout=300)

    stage = plan_mod.PlanStage(subprocess_run=_run)
    with pytest.raises(SubagentTimeout):
        stage._invoke_subagent("planner", "prompt")


def test_invoke_subagent_non_object_json_direct() -> None:
    """`_invoke_subagent` raises SubagentMalformedOutput when stdout is a JSON list."""

    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        return subprocess.CompletedProcess(cmd, 0, stdout="[1, 2, 3]", stderr="")

    stage = plan_mod.PlanStage(subprocess_run=_run)
    with pytest.raises(SubagentMalformedOutput):
        stage._invoke_subagent("planner", "prompt")
