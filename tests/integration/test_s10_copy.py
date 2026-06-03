"""Integration tests for the `s10_copy` stage + the `copy` CLI verb (Slice 19).

Owned TCs (Section 13 + Section 20):
- TC-13.1: happy path — `social-copywriter` `claude -p` mocked → three Markdown
           files with correct lengths (twitter 3-8 tweets each <= 280 chars,
           linkedin 600-1200 words, blog 1200-2000 words).
- TC-13.2: each file opens with the brief's chosen hook template (first non-blank
           line CONTAINS `hooks.render(key, entry)`).
- TC-13.3: twitter has no `**`, Unicode bold present.
- TC-13.4: linkedin uses `->`/`>` bullets, no `- `/`* ` Markdown list markers.
- TC-13.5: sub-agent TimeoutExpired → SubagentTimeout, FAILED, no `.md` files.
- TC-13.6: blog with only ~100 words → validation fails citing word-count,
           FAILED, no `.md` files; too-many/too-few tweets + tweet > 280 chars +
           linkedin out of range each fail the same way.
- TC-13.9: `s10_copy` reads `03_brand/voice.md` (not the raw `_brand/<slug>/`);
           the voice.md path is passed to the sub-agent and check_inputs passes
           with only the `03_brand/` copy present.

The single `claude -p` call is mocked through the stage's injected
`subprocess.run`. No real `claude` / network / browser. The project is driven to
"04_plan done+approved" via the runner with mocked enrich/brand/plan agents.
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
import shipcast.stages.s10_copy as copy_mod
from shipcast.manifest import Manifest, StageStatus
from shipcast.marketing import hooks

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
ENTRY_NAME = "Add CSV export"

#: Brief hooks used throughout (matches TC-13.2's example assignment).
HOOK_X = "we_just_shipped"
HOOK_LINKEDIN = "before_after"
HOOK_BLOG = "problem_aha"

#: The entry mapping `hooks.render` sees — MUST mirror the picked entry.json
#: that `s01_pick` writes from the canonical changelog fixture (name + summary),
#: so the hooks rendered in the fixtures below equal what `s10_copy` computes.
_ENTRY_FOR_HOOK: dict[str, Any] = {
    "name": ENTRY_NAME,
    "summary": "Users can now download their report as a spreadsheet file.",
}


# --------------------------------------------------------------------------- #
# CopyBundle JSON fixtures (pinned mock sub-agent stdout)
# --------------------------------------------------------------------------- #

# Unicode mathematical bold "ship" (U+1D400 block), for the no-`**` assertion.
_UNI_BOLD_SHIP = "\U0001d600\U0001d691\U0001d692\U0001d699"


def _twitter(*, n_tweets: int = 4, overlong: bool = False) -> str:
    """A numbered Twitter thread opening with the rendered `x` hook.

    Tweet 1 contains the hook; tweets use Unicode bold and never Markdown `**`.
    """
    hook = hooks.render(HOOK_X, _ENTRY_FOR_HOOK)
    lines = [f"1/ {hook} {_UNI_BOLD_SHIP} it."]
    for i in range(2, n_tweets):
        lines.append(f"{i}/ Point {i}: one idea per tweet, {_UNI_BOLD_SHIP}.")
    if overlong:
        # Last tweet blows past 280 chars.
        lines.append(f"{n_tweets}/ " + "x" * 300)
    else:
        lines.append(
            f"{n_tweets}/ Try it today. If this helped, RT the first tweet."
        )
    return "\n".join(lines)


def _linkedin(*, n_words: int = 700) -> str:
    """A LinkedIn post opening with the rendered `linkedin` hook, n_words long."""
    hook = hooks.render(HOOK_LINKEDIN, _ENTRY_FOR_HOOK)
    body_words = ["value"] * max(0, n_words - len(hook.split()) - 12)
    return (
        f"{hook}\n\n"
        + " ".join(body_words)
        + "\n\n→ one click downloads your whole report.\n"
        + "▸ streams large datasets without timing out.\n\n"
        + "What would you automate with it?\n\n"
        + "#ship #build #devtools"
    )


def _blog(*, n_words: int = 1300) -> str:
    """A blog post opening with the rendered `blog` hook, n_words long."""
    hook = hooks.render(HOOK_BLOG, _ENTRY_FOR_HOOK)
    filler = ["word"] * max(0, n_words - len(hook.split()) - 20)
    return (
        f"{hook}\n\n"
        "**TL;DR**\n- one\n- two\n- three\n\n"
        + " ".join(filler)
        + "\n\nTry it. Feedback welcome. — the team"
    )


def _bundle(
    *,
    twitter: str | None = None,
    linkedin: str | None = None,
    blog: str | None = None,
) -> dict[str, str]:
    return {
        "twitter_thread": _twitter() if twitter is None else twitter,
        "linkedin": _linkedin() if linkedin is None else linkedin,
        "blog": _blog() if blog is None else blog,
    }


def _marker_stdout(bundle: dict[str, str]) -> str:
    """Render a CopyBundle dict as the copywriter's marker-delimited stdout."""
    return (
        f"<<<TWITTER>>>\n{bundle['twitter_thread']}\n"
        f"<<<LINKEDIN>>>\n{bundle['linkedin']}\n"
        f"<<<BLOG>>>\n{bundle['blog']}\n<<<END>>>\n"
    )


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


def _valid_brief() -> dict[str, Any]:
    return {
        "hook_template_per_channel": {
            "x": HOOK_X,
            "linkedin": HOOK_LINKEDIN,
            "blog": HOOK_BLOG,
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


def _drive_to_plan_approved(
    projects_root: Path,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run pick → enrich → brand → plan and approve each, so s10_copy's gate passes.

    The picked entry.json carries `name`/`summary` == ENTRY_NAME so the hook
    renderings used by the copy fixtures match what `s10_copy` computes.
    """
    _seed_brand_pack(projects_root)

    # pick
    result = runner.invoke(
        cli.app,
        [*_root(projects_root), "pick", str(target_repo), "--entry", ENTRY_NAME],
    )
    assert result.exit_code == 0, result.output

    input_path = projects_root / SLUG / "input.yaml"
    data: dict[str, Any] = {
        "repo_path": str(target_repo),
        "entry_heading": ENTRY_NAME,
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

    # brand (mocked gemini image only)
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

    # plan (chained planner + brand-guardian both mocked to the same valid brief)
    def _fake_plan_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        assert cmd[0] == "claude", f"unexpected subprocess: {cmd!r}"
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(_valid_brief()), stderr=""
        )

    monkeypatch.setattr(plan_mod.subprocess, "run", _fake_plan_run)
    result = runner.invoke(cli.app, [*_root(projects_root), "plan", SLUG])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "04_plan"])
    assert result.exit_code == 0, result.output


def _install_copy_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    timeout: bool = False,
    returncode: int = 0,
    stderr: str = "",
) -> MagicMock:
    """Patch the copy stage's `subprocess.run` to fake the single claude call."""
    calls = MagicMock()

    def _fake_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        calls(cmd)
        assert cmd[0] == "claude", f"unexpected subprocess: {cmd!r}"
        # Plain `claude -p` (no --agent) — the copy call uses the default agent.
        assert "--agent" not in cmd
        if timeout:
            raise subprocess.TimeoutExpired(cmd, timeout=300)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(copy_mod.subprocess, "run", _fake_run)
    return calls


def _copy_dir(projects_root: Path) -> Path:
    return projects_root / SLUG / "10_copy"


# --------------------------------------------------------------------------- #
# TC-13.1 — happy path: three files, correct lengths
# --------------------------------------------------------------------------- #


def test_tc_13_1_happy_path_three_files_correct_lengths(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-13.1: mocked copywriter → 3 files with correct tweet count / word counts."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_copy_subprocess(monkeypatch, stdout=_marker_stdout(_bundle()))

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code == 0, result.output

    twitter = (_copy_dir(projects_root) / "twitter_thread.md").read_text(
        encoding="utf-8"
    )
    linkedin = (_copy_dir(projects_root) / "linkedin.md").read_text(encoding="utf-8")
    blog = (_copy_dir(projects_root) / "blog.md").read_text(encoding="utf-8")

    tweets = [ln for ln in twitter.splitlines() if ln.strip()]
    assert 3 <= len(tweets) <= 8
    for tweet in tweets:
        assert len(tweet) <= 280

    assert 600 <= len(linkedin.split()) <= 1200
    assert 1200 <= len(blog.split()) <= 2000

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["10_copy"]
    assert rec.status == StageStatus.DONE
    assert "10_copy/twitter_thread.md" in rec.outputs
    assert "10_copy/linkedin.md" in rec.outputs
    assert "10_copy/blog.md" in rec.outputs


# --------------------------------------------------------------------------- #
# TC-13.2 — each file opens with the channel's hook
# --------------------------------------------------------------------------- #


def test_tc_13_2_each_file_opens_with_channel_hook(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-13.2: first non-blank line of each file CONTAINS hooks.render(key, entry)."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_copy_subprocess(monkeypatch, stdout=_marker_stdout(_bundle()))

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code == 0, result.output

    # The picked entry.json that s10_copy renders the hooks against.
    entry_json = (projects_root / SLUG / "01_pick" / "entry.json").read_text(
        encoding="utf-8"
    )
    entry = json.loads(entry_json)

    for filename, channel in (
        ("twitter_thread.md", "x"),
        ("linkedin.md", "linkedin"),
        ("blog.md", "blog"),
    ):
        body = (_copy_dir(projects_root) / filename).read_text(encoding="utf-8")
        first_line = next(ln for ln in body.splitlines() if ln.strip())
        brief = json.loads(
            (projects_root / SLUG / "04_plan" / "brief.json").read_text(
                encoding="utf-8"
            )
        )
        key = brief["hook_template_per_channel"][channel]
        assert hooks.render(key, entry) in first_line


# --------------------------------------------------------------------------- #
# TC-13.3 — twitter: no `**`, Unicode bold present
# --------------------------------------------------------------------------- #


def test_tc_13_3_twitter_unicode_bold_no_markdown(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-13.3: twitter_thread.md has no `**`; Unicode mathematical bold present."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_copy_subprocess(monkeypatch, stdout=_marker_stdout(_bundle()))

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code == 0, result.output

    twitter = (_copy_dir(projects_root) / "twitter_thread.md").read_text(
        encoding="utf-8"
    )
    assert "**" not in twitter
    assert any("\U0001d400" <= ch <= "\U0001d7ff" for ch in twitter)


# --------------------------------------------------------------------------- #
# TC-13.4 — linkedin: Unicode bullets, no Markdown list markers
# --------------------------------------------------------------------------- #


def test_tc_13_4_linkedin_unicode_bullets_no_markdown_markers(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-13.4: linkedin.md uses `->`/`>` bullets, no leading `- `/`* ` markers."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_copy_subprocess(monkeypatch, stdout=_marker_stdout(_bundle()))

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code == 0, result.output

    linkedin = (_copy_dir(projects_root) / "linkedin.md").read_text(encoding="utf-8")
    assert "→" in linkedin or "▸" in linkedin
    for line in linkedin.splitlines():
        assert not line.lstrip().startswith("- ")
        assert not line.lstrip().startswith("* ")


# --------------------------------------------------------------------------- #
# TC-13.5 — sub-agent timeout → SubagentTimeout, FAILED, no files
# --------------------------------------------------------------------------- #


def test_tc_13_5_subagent_timeout_no_files(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-13.5: copywriter TimeoutExpired → SubagentTimeout, FAILED, no .md files."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_copy_subprocess(monkeypatch, timeout=True)

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code != 0, result.output

    assert not (_copy_dir(projects_root) / "twitter_thread.md").exists()
    assert not (_copy_dir(projects_root) / "linkedin.md").exists()
    assert not (_copy_dir(projects_root) / "blog.md").exists()

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["10_copy"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentTimeout"


# --------------------------------------------------------------------------- #
# TC-13.6 — length / structure violations → FAILED, no files
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bundle_kwargs",
    [
        {"blog": "word " * 100},  # blog only ~100 words
        {"linkedin": "short " * 50},  # linkedin under 600 words
        {"linkedin": "word " * 1500},  # linkedin over 1200 words
        {"blog": "word " * 2500},  # blog over 2000 words
    ],
    ids=["blog_too_short", "linkedin_too_short", "linkedin_too_long", "blog_too_long"],
)
def test_tc_13_6_word_count_violation_fails(
    projects_root: Path,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundle_kwargs: dict[str, str],
) -> None:
    """TC-13.6: out-of-range word counts → stage FAILED, no .md files written."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    # The out-of-range fields still need a valid hook opening so the failure is
    # unambiguously the length check — prepend the rendered hook.
    if "linkedin" in bundle_kwargs:
        bundle_kwargs["linkedin"] = (
            hooks.render(HOOK_LINKEDIN, _ENTRY_FOR_HOOK) + "\n\n" + bundle_kwargs["linkedin"]
        )
    if "blog" in bundle_kwargs:
        bundle_kwargs["blog"] = (
            hooks.render(HOOK_BLOG, _ENTRY_FOR_HOOK) + "\n\n" + bundle_kwargs["blog"]
        )
    _install_copy_subprocess(monkeypatch, stdout=_marker_stdout(_bundle(**bundle_kwargs)))

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code != 0, result.output

    assert not (_copy_dir(projects_root) / "twitter_thread.md").exists()
    assert not (_copy_dir(projects_root) / "linkedin.md").exists()
    assert not (_copy_dir(projects_root) / "blog.md").exists()

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["10_copy"].status == StageStatus.FAILED


@pytest.mark.parametrize(
    "twitter_kwargs",
    [
        {"n_tweets": 2},  # too few tweets
        {"n_tweets": 9},  # too many tweets
        {"overlong": True},  # a tweet > 280 chars
    ],
    ids=["too_few_tweets", "too_many_tweets", "tweet_over_280"],
)
def test_tc_13_6_twitter_structure_violation_fails(
    projects_root: Path,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    twitter_kwargs: dict[str, Any],
) -> None:
    """TC-13.6 (twitter): bad tweet count or an over-280-char tweet → FAILED."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    _install_copy_subprocess(
        monkeypatch, stdout=_marker_stdout(_bundle(twitter=_twitter(**twitter_kwargs)))
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code != 0, result.output
    assert not (_copy_dir(projects_root) / "twitter_thread.md").exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["10_copy"].status == StageStatus.FAILED


def test_tc_13_3_markdown_bold_in_twitter_fails(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-13.3 (negative): a `**bold**` Twitter thread fails CopyBundle validation."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    hook = hooks.render(HOOK_X, _ENTRY_FOR_HOOK)
    bad_twitter = f"1/ {hook} **bold**\n2/ point\n3/ Try it. RT the first tweet."
    _install_copy_subprocess(
        monkeypatch, stdout=_marker_stdout(_bundle(twitter=bad_twitter))
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code != 0, result.output
    assert not (_copy_dir(projects_root) / "twitter_thread.md").exists()


# --------------------------------------------------------------------------- #
# Missing-hook opening → FAILED (FR-12.4 enforcement)
# --------------------------------------------------------------------------- #


def test_missing_hook_opening_fails(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel whose first line omits the chosen hook → FAILED, no files."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    # Twitter thread that is length-valid but does NOT open with the x hook.
    no_hook_twitter = (
        "1/ Random opening with no hook here.\n"
        "2/ Second point.\n"
        "3/ Try it. If this helped, RT the first tweet."
    )
    _install_copy_subprocess(
        monkeypatch, stdout=_marker_stdout(_bundle(twitter=no_hook_twitter))
    )

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code != 0, result.output
    assert not (_copy_dir(projects_root) / "twitter_thread.md").exists()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["10_copy"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "SubagentMalformedOutput"


# --------------------------------------------------------------------------- #
# TC-13.9 — reads 03_brand/voice.md (not the raw _brand/<slug>/ pack)
# --------------------------------------------------------------------------- #


def test_tc_13_9_reads_brand_copy_of_voice_md(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-13.9: voice.md is read from 03_brand/; the sub-agent prompt carries it."""
    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)

    # Confirm the canonical 03_brand/voice.md exists (s03_brand copied it).
    brand_voice = projects_root / SLUG / "03_brand" / "voice.md"
    assert brand_voice.is_file()
    sentinel = "SENTINEL-VOICE-LINE"
    brand_voice.write_text(
        f"# Voice\ncaption_mode: chip\n{sentinel}\n", encoding="utf-8"
    )

    captured: dict[str, str] = {}

    def _fake_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        assert cmd[0] == "claude"
        captured["prompt"] = cmd[-1]
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_marker_stdout(_bundle()), stderr=""
        )

    monkeypatch.setattr(copy_mod.subprocess, "run", _fake_run)

    result = runner.invoke(cli.app, [*_root(projects_root), "copy", SLUG])
    assert result.exit_code == 0, result.output

    # The 03_brand/voice.md content reached the sub-agent prompt.
    assert sentinel in captured["prompt"]
    assert "03_brand/voice.md" in captured["prompt"]


def test_tc_13_9_check_inputs_passes_with_only_brand_copy(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-13.9: check_inputs needs only 04_plan outputs — no raw _brand reference.

    Removing the raw `_brand/<slug>/voice.md` must NOT break `s10_copy`: the stage
    reads the `03_brand/` copy, so its gate still passes.
    """
    from shipcast.project import Project

    _drive_to_plan_approved(projects_root, target_repo, monkeypatch)
    raw_voice = projects_root / "_brand" / BRAND_SLUG / "voice.md"
    assert raw_voice.is_file()
    raw_voice.unlink()

    project = Project.load(projects_root, SLUG)
    # No FileNotFoundError / StageInputMissing — 04_plan outputs are present.
    copy_mod.CopyStage().check_inputs(project)


# --------------------------------------------------------------------------- #
# Direct-unit error-path coverage (no full runner)
# --------------------------------------------------------------------------- #


def test_invoke_subagent_non_zero_exit_direct() -> None:
    """`_invoke_subagent` raises SubagentFailed on a non-zero exit."""
    from shipcast.errors import SubagentFailed

    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    stage = copy_mod.CopyStage(subprocess_run=_run)
    with pytest.raises(SubagentFailed):
        stage._invoke_subagent("social-copywriter", "prompt")


def test_parse_sections_missing_marker_direct() -> None:
    """`_parse_sections` raises SubagentMalformedOutput when a marker is absent."""
    from shipcast.errors import SubagentMalformedOutput

    stage = copy_mod.CopyStage()
    # Missing the <<<BLOG>>> marker.
    bad = "<<<TWITTER>>>\na\n<<<LINKEDIN>>>\nb\n<<<END>>>\n"
    with pytest.raises(SubagentMalformedOutput):
        stage._parse_sections(bad)


def test_parse_sections_out_of_order_direct() -> None:
    """`_parse_sections` raises when the markers appear out of order."""
    from shipcast.errors import SubagentMalformedOutput

    stage = copy_mod.CopyStage()
    bad = "<<<LINKEDIN>>>\nb\n<<<TWITTER>>>\na\n<<<BLOG>>>\nc\n<<<END>>>\n"
    with pytest.raises(SubagentMalformedOutput):
        stage._parse_sections(bad)
