"""Integration tests for the `s02_enrich` stage + the `enrich` CLI verb (Slice 7).

Owned TCs:
- TC-5.1:  happy path — gh/git + Playwright + Gemini multimodal + ba-analyst all
           mocked → schema-valid `02_enrich/context.json` with list[str] pr_links,
           dict diff_stats, non-empty narrative, ≥1 PNG screenshot path.
- TC-5.2:  `live_url=None` → screenshots==[], narrative non-empty, playwright
           never called.
- TC-5.4:  ba-analyst `subprocess.TimeoutExpired` → SubagentTimeout, FAILED.
- TC-5.5:  ba-analyst non-zero exit → stderr captured in error.
- TC-5.6:  ba-analyst non-JSON stdout → SubagentMalformedOutput, FAILED.
- TC-5.7:  multimodal raises GeminiRateLimited → FAILED, no context.json.
- TC-5.8:  Playwright screenshot raises PlaywrightTimeout → FAILED.
- TC-5.9/TC-20.4: Finding 3 — NO `narrative.md` on disk; `context.json.narrative`
           is the sole copy; every written file appears in `outputs`.

All external clients are injected via a mock `clients_factory`; the `claude -p`
subprocess and `gh`/`git` calls are mocked. No real API / network / subprocess.
`live_url` validation's DNS lookup (`socket.getaddrinfo`) is monkeypatched to a
public IP so no real resolution occurs.
"""

from __future__ import annotations

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
from shipcast.manifest import Manifest, StageStatus
from shipcast.schemas import EnrichedContext

runner = CliRunner()

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "repos" / "example_min"
_CHANGELOG = (_FIXTURES / "CHANGELOG.md").read_text(encoding="utf-8")

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

SLUG = "example-project--add-csv-export"


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


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `live_url` DNS resolution to a public address (no real network)."""

    def _fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [(2, 1, 6, "", ("93.184.216.34", 0))]  # example.com, public

    import socket

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


def _pick_and_approve(projects_root: Path, target_repo: Path) -> None:
    """Run `pick` (create mode) then `approve 01_pick` so enrich's gate is satisfied."""
    result = runner.invoke(
        cli.app,
        [*_root(projects_root), "pick", str(target_repo), "--entry", "Add CSV export"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "01_pick"])
    assert result.exit_code == 0, result.output


def _write_input_yaml(
    projects_root: Path,
    target_repo: Path,
    *,
    live_url: str | None,
    walkthrough: list[dict[str, Any]] | None,
) -> None:
    """Overwrite the project's input.yaml to add live_url / feature_walkthrough."""
    input_path = projects_root / SLUG / "input.yaml"
    data: dict[str, Any] = {
        "repo_path": str(target_repo),
        "entry_heading": "Add CSV export",
        "brand_slug": "test-brand",
        "video_mode": "standard",
    }
    if live_url is not None:
        data["live_url"] = live_url
    if walkthrough is not None:
        data["feature_walkthrough"] = walkthrough
    input_path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _install_clients_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gemini: MagicMock,
    playwright: MagicMock | None,
) -> None:
    """Patch the stage's default clients factory to return the given mocks."""

    def _factory(project: Any) -> Any:
        class _Bundle:
            def __init__(self) -> None:
                self.gemini = gemini
                self.playwright = playwright

        return _Bundle()

    monkeypatch.setattr(enrich_mod, "_default_clients_factory", _factory)


def _install_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ba_stdout: str = '{"angle": "developer-velocity"}',
    ba_returncode: int = 0,
    ba_stderr: str = "",
    ba_timeout: bool = False,
) -> None:
    """Patch `subprocess.run` to fake gh / git / claude calls (no real subprocess)."""

    def _fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        prog = cmd[0]
        if prog == "gh":
            return subprocess.CompletedProcess(
                cmd, 0, stdout='[{"url": "https://github.com/x/y/pull/1"}]', stderr=""
            )
        if prog == "git":
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=" 3 files changed, 42 insertions(+), 7 deletions(-)\n",
                stderr="",
            )
        if prog == "claude":
            if ba_timeout:
                raise subprocess.TimeoutExpired(cmd, timeout=300)
            return subprocess.CompletedProcess(
                cmd, ba_returncode, stdout=ba_stdout, stderr=ba_stderr
            )
        raise AssertionError(f"unexpected subprocess call: {cmd!r}")

    monkeypatch.setattr(subprocess, "run", _fake_run)


# --------------------------------------------------------------------------- #
# TC-5.1 — happy path
# --------------------------------------------------------------------------- #


def test_tc_5_1_happy_path(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-5.1: all three sub-steps succeed → schema-valid context.json."""
    _pick_and_approve(projects_root, target_repo)
    _public_dns(monkeypatch)
    _write_input_yaml(
        projects_root,
        target_repo,
        live_url="https://example.com",
        walkthrough=[{"action": "goto"}, {"action": "screenshot"}],
    )

    shot = projects_root / SLUG / "02_enrich" / "_raw_shot.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(TINY_PNG)

    gemini = MagicMock()
    gemini.multimodal.return_value = "A compelling marketing narrative."
    playwright = MagicMock()
    playwright.screenshot_feature.return_value = [shot]
    _install_clients_factory(monkeypatch, gemini=gemini, playwright=playwright)
    _install_subprocess(monkeypatch)

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == 0, result.output

    context_path = projects_root / SLUG / "02_enrich" / "context.json"
    assert context_path.is_file()
    ctx = EnrichedContext.model_validate_json(context_path.read_text(encoding="utf-8"))
    assert isinstance(ctx.pr_links, list) and all(isinstance(p, str) for p in ctx.pr_links)
    assert ctx.pr_links == ["https://github.com/x/y/pull/1"]
    assert isinstance(ctx.diff_stats, dict)
    assert ctx.diff_stats["files_changed"] == 3
    assert ctx.narrative.strip()
    assert len(ctx.screenshots) >= 1
    assert all(s.endswith(".png") for s in ctx.screenshots)

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["02_enrich"]
    assert rec.status == StageStatus.DONE
    # All written files (context.json + the screenshot) appear in outputs.
    assert "02_enrich/context.json" in rec.outputs
    assert any(o.endswith(".png") for o in rec.outputs)
    gemini.multimodal.assert_called_once()
    playwright.screenshot_feature.assert_called_once()


# --------------------------------------------------------------------------- #
# TC-5.2 — live_url absent → playwright skipped
# --------------------------------------------------------------------------- #


def test_tc_5_2_live_url_absent_skips_playwright(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-5.2: live_url=None → screenshots==[], playwright never called, narrative set."""
    _pick_and_approve(projects_root, target_repo)
    _write_input_yaml(projects_root, target_repo, live_url=None, walkthrough=None)

    gemini = MagicMock()
    gemini.multimodal.return_value = "Narrative without screenshots."
    playwright = MagicMock()
    _install_clients_factory(monkeypatch, gemini=gemini, playwright=playwright)
    _install_subprocess(monkeypatch)

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == 0, result.output

    ctx = EnrichedContext.model_validate_json(
        (projects_root / SLUG / "02_enrich" / "context.json").read_text(encoding="utf-8")
    )
    assert ctx.screenshots == []
    assert ctx.narrative.strip()
    playwright.screenshot_feature.assert_not_called()
    # multimodal still called (with no images).
    gemini.multimodal.assert_called_once()
    called_images = gemini.multimodal.call_args.args[1]
    assert called_images == []


# --------------------------------------------------------------------------- #
# TC-5.3 — walkthrough absent but live_url present → single viewport screenshot
# --------------------------------------------------------------------------- #


def test_tc_5_3_no_walkthrough_but_url_present(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-5.3: live_url set, feature_walkthrough=None → playwright called with [].

    The Slice-8 client performs a single viewport screenshot when handed an
    empty walkthrough; the stage just delegates and records the returned path.
    """
    _pick_and_approve(projects_root, target_repo)
    _public_dns(monkeypatch)
    _write_input_yaml(
        projects_root, target_repo, live_url="https://example.com", walkthrough=None
    )

    shot = projects_root / SLUG / "02_enrich" / "_viewport.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(TINY_PNG)

    gemini = MagicMock()
    gemini.multimodal.return_value = "Narrative."
    playwright = MagicMock()
    playwright.screenshot_feature.return_value = [shot]
    _install_clients_factory(monkeypatch, gemini=gemini, playwright=playwright)
    _install_subprocess(monkeypatch)

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == 0, result.output

    ctx = EnrichedContext.model_validate_json(
        (projects_root / SLUG / "02_enrich" / "context.json").read_text(encoding="utf-8")
    )
    assert len(ctx.screenshots) == 1
    # Called with an empty walkthrough list (no step-by-step automation).
    playwright.screenshot_feature.assert_called_once()
    assert playwright.screenshot_feature.call_args.args[1] == []


# --------------------------------------------------------------------------- #
# TC-5.4 — ba-analyst timeout
# --------------------------------------------------------------------------- #


def test_tc_5_4_subagent_timeout(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-5.4: ba-analyst TimeoutExpired → SubagentTimeout, status FAILED."""
    _pick_and_approve(projects_root, target_repo)
    _write_input_yaml(projects_root, target_repo, live_url=None, walkthrough=None)

    gemini = MagicMock()
    gemini.multimodal.return_value = "Narrative."
    _install_clients_factory(monkeypatch, gemini=gemini, playwright=None)
    _install_subprocess(monkeypatch, ba_timeout=True)

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["02_enrich"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentTimeout"


# --------------------------------------------------------------------------- #
# TC-5.5 — ba-analyst non-zero exit
# --------------------------------------------------------------------------- #


def test_tc_5_5_subagent_nonzero_exit_captures_stderr(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-5.5: non-zero exit → stage failed, stderr captured in error message."""
    _pick_and_approve(projects_root, target_repo)
    _write_input_yaml(projects_root, target_repo, live_url=None, walkthrough=None)

    gemini = MagicMock()
    gemini.multimodal.return_value = "Narrative."
    _install_clients_factory(monkeypatch, gemini=gemini, playwright=None)
    _install_subprocess(monkeypatch, ba_returncode=1, ba_stderr="boom")

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["02_enrich"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentFailed"
    assert "boom" in rec.error.message


# --------------------------------------------------------------------------- #
# TC-5.6 — ba-analyst malformed output
# --------------------------------------------------------------------------- #


def test_tc_5_6_subagent_malformed_output(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-5.6: non-JSON stdout → SubagentMalformedOutput, FAILED."""
    _pick_and_approve(projects_root, target_repo)
    _write_input_yaml(projects_root, target_repo, live_url=None, walkthrough=None)

    gemini = MagicMock()
    gemini.multimodal.return_value = "Narrative."
    _install_clients_factory(monkeypatch, gemini=gemini, playwright=None)
    _install_subprocess(monkeypatch, ba_stdout="not json at all")

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["02_enrich"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentMalformedOutput"


# --------------------------------------------------------------------------- #
# TC-5.7 — Gemini rate limited
# --------------------------------------------------------------------------- #


def test_tc_5_7_gemini_rate_limited(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-5.7: multimodal raises GeminiRateLimited → FAILED, no context.json."""
    from shipcast.errors import GeminiRateLimited

    _pick_and_approve(projects_root, target_repo)
    _write_input_yaml(projects_root, target_repo, live_url=None, walkthrough=None)

    gemini = MagicMock()
    gemini.multimodal.side_effect = GeminiRateLimited("rate limited")
    _install_clients_factory(monkeypatch, gemini=gemini, playwright=None)
    _install_subprocess(monkeypatch)

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    assert not (projects_root / SLUG / "02_enrich" / "context.json").exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["02_enrich"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "GeminiRateLimited"


# --------------------------------------------------------------------------- #
# TC-5.8 — Playwright timeout
# --------------------------------------------------------------------------- #


def test_tc_5_8_playwright_timeout(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-5.8: playwright screenshot raises PlaywrightTimeout → FAILED."""
    from shipcast.errors import PlaywrightTimeout

    _pick_and_approve(projects_root, target_repo)
    _public_dns(monkeypatch)
    _write_input_yaml(
        projects_root,
        target_repo,
        live_url="https://example.com",
        walkthrough=[{"action": "goto"}],
    )

    gemini = MagicMock()
    gemini.multimodal.return_value = "Narrative."
    playwright = MagicMock()
    playwright.screenshot_feature.side_effect = PlaywrightTimeout("nav timed out")
    _install_clients_factory(monkeypatch, gemini=gemini, playwright=playwright)
    _install_subprocess(monkeypatch)

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["02_enrich"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "PlaywrightTimeout"
    # multimodal never reached (screenshots fail first).
    gemini.multimodal.assert_not_called()


# --------------------------------------------------------------------------- #
# TC-5.9 / TC-20.4 — Finding 3 single source of truth
# --------------------------------------------------------------------------- #


def test_tc_5_9_no_narrative_md_single_source_of_truth(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-5.9/TC-20.4: NO narrative.md; context.json.narrative is the sole copy.

    Also asserts every file on disk under 02_enrich/ is a declared output (no
    undeclared file left behind).
    """
    _pick_and_approve(projects_root, target_repo)
    _write_input_yaml(projects_root, target_repo, live_url=None, walkthrough=None)

    narrative_text = "The one and only narrative copy."
    gemini = MagicMock()
    gemini.multimodal.return_value = narrative_text
    _install_clients_factory(monkeypatch, gemini=gemini, playwright=None)
    _install_subprocess(monkeypatch)

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == 0, result.output

    stage_dir = projects_root / SLUG / "02_enrich"
    # No narrative.md anywhere.
    assert not (stage_dir / "narrative.md").exists()
    assert list(stage_dir.rglob("narrative.md")) == []

    # The narrative is accessible only via context.json.narrative.
    ctx = EnrichedContext.model_validate_json(
        (stage_dir / "context.json").read_text(encoding="utf-8")
    )
    assert ctx.narrative == narrative_text

    # Every STAGE-WRITTEN file under 02_enrich/ appears in manifest outputs.
    # `.gitkeep` is template scaffolding (created at project-template copy, not
    # by the stage) so it is legitimately not a declared output.
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    declared = set(m.stages["02_enrich"].outputs)
    for f in stage_dir.rglob("*"):
        if f.is_file() and f.name != ".gitkeep":
            rel = str(f.relative_to(projects_root / SLUG))
            assert rel in declared, f"undeclared file on disk: {rel}"
