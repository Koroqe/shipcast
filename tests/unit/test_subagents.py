"""Sub-agent snapshot tests (testing.md "Sub-agent snapshot tests").

Each project-specific sub-agent ships a fixed-scenario snapshot test that mocks
the `claude -p` subprocess with pinned stdout and asserts the parsing of the
expected output structure (JSON shape + length constraints). NO real `claude`
calls.

Slice 11 lands the `brand-guardian` snapshot (TC-22.5). Slices 12 and 19 extend
this module with `demo-script-writer` and `social-copywriter`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import shipcast.stages.s04_plan as plan_mod
from shipcast.schemas import MarketingBrief

# --------------------------------------------------------------------------- #
# brand-guardian (Slice 11, TC-22.5)
# --------------------------------------------------------------------------- #

#: Pinned brand-guardian stdout: a fully-conformant MarketingBrief JSON object.
_GUARDIAN_SNAPSHOT: dict[str, Any] = {
    "hook_template_per_channel": {
        "x": "we_just_shipped",
        "linkedin": "before_after",
        "blog": "problem_aha",
    },
    "ctas": ["Try it now"],
    "video_beats": [
        {
            "image_prompt": f"beat {i} visual",
            "narration": f"beat {i} narration",
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


def _stub_run(stdout: str) -> Any:
    """Return a fake `subprocess.run` that yields `stdout` with exit 0."""

    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return _run


def test_brand_guardian_snapshot_parses_to_marketing_brief() -> None:
    """TC-22.5: pinned brand-guardian stdout parses to a valid MarketingBrief.

    Asserts the JSON shape + the HARD length constraints (video_beats == 4,
    carousel_beats == 4) and that every hook value is a catalog key.
    """
    stage = plan_mod.PlanStage(
        subprocess_run=_stub_run(json.dumps(_GUARDIAN_SNAPSHOT))
    )

    parsed = stage._invoke_subagent("brand-guardian", "guard this draft")

    # Parsed JSON shape — exactly the MarketingBrief keys, nothing extra.
    assert set(parsed.keys()) == {
        "hook_template_per_channel",
        "ctas",
        "video_beats",
        "carousel_beats",
        "has_stat_card",
        "has_code_screenshot",
    }

    brief = MarketingBrief.model_validate(parsed)
    assert len(brief.video_beats) == 4
    assert len(brief.carousel_beats) == 4
    assert brief.ctas

    from shipcast.marketing import hooks

    for value in brief.hook_template_per_channel.values():
        assert value in hooks.KEYS


def test_brand_guardian_snapshot_rejects_bad_length() -> None:
    """A snapshot with carousel_beats != 4 fails MarketingBrief validation."""
    bad = dict(_GUARDIAN_SNAPSHOT)
    bad["carousel_beats"] = _GUARDIAN_SNAPSHOT["carousel_beats"][:3]
    stage = plan_mod.PlanStage(subprocess_run=_stub_run(json.dumps(bad)))

    parsed = stage._invoke_subagent("brand-guardian", "guard this draft")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MarketingBrief.model_validate(parsed)


# --------------------------------------------------------------------------- #
# demo-script-writer (Slice 12, TC-8.8 / TC-22.5)
# --------------------------------------------------------------------------- #

import shipcast.stages.s05_script as script_mod  # noqa: E402
from shipcast.schemas import Storyboard  # noqa: E402

#: Pinned demo-script-writer stdout: a conformant 4-beat Storyboard JSON object.
_SCRIPT_SNAPSHOT: dict[str, Any] = {
    "beats": [
        {
            "image_prompt": f"beat {i} visual",
            "narration": f"beat {i} narration",
            "duration_sec": 4.0,
        }
        for i in range(4)
    ]
}


def test_demo_script_writer_snapshot_parses_to_storyboard() -> None:
    """TC-8.8 / TC-22.5: pinned demo-script-writer stdout parses to a Storyboard.

    Asserts the JSON shape (single `beats` key), the beat count is in [4, 6],
    and every beat carries all three required fields with a 3-5 s duration.
    """
    stage = script_mod.ScriptStage(
        subprocess_run=_stub_run(json.dumps(_SCRIPT_SNAPSHOT))
    )

    parsed = stage._invoke_subagent("demo-script-writer", "draft the storyboard")

    # Parsed JSON shape — exactly the Storyboard keys, nothing extra.
    assert set(parsed.keys()) == {"beats"}

    storyboard = Storyboard.model_validate(parsed)
    assert 4 <= len(storyboard.beats) <= 6
    for beat in storyboard.beats:
        assert beat.image_prompt
        assert beat.narration
        assert 3.0 <= beat.duration_sec <= 5.0


def test_demo_script_writer_snapshot_rejects_bad_beat_count() -> None:
    """A snapshot with 3 beats fails Storyboard's 4-6 beat validation."""
    bad = {"beats": _SCRIPT_SNAPSHOT["beats"][:3]}
    stage = script_mod.ScriptStage(subprocess_run=_stub_run(json.dumps(bad)))

    parsed = stage._invoke_subagent("demo-script-writer", "draft the storyboard")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Storyboard.model_validate(parsed)


# --------------------------------------------------------------------------- #
# social-copywriter (Slice 18, structure contract — TC-22.5)
# --------------------------------------------------------------------------- #
#
# The `social-copywriter` sub-agent and the `s10_copy` stage that drives it land
# in Slice 19. Slice 18 lands this forward-looking SNAPSHOT of the EXPECTED
# output STRUCTURE the copywriter must emit, parsed from pinned `claude -p`
# stdout with NO real LLM call and NO dependency on the not-yet-built stage. It
# pins the three-field CopyBundle shape (twitter_thread / linkedin / blog) plus
# the load-bearing Twitter formatting constraints (3-8 numbered tweets, each
# <= 280 chars, Unicode bold instead of Markdown `**bold**`). When Slice 19 wires
# the real stage, TC-13.8 extends this with the live parsing path.

#: Pinned social-copywriter stdout: a conformant CopyBundle JSON object. Twitter
#: uses Unicode-bold (U+1D400 block) and contains NO Markdown `**` markers.
_COPYWRITER_SNAPSHOT: dict[str, Any] = {
    "twitter_thread": (
        "1/ \U0001d5ea\U0001d5f2 \U0001d5f7\U0001d602\U0001d600\U0001d601 "
        "shipped CSV export.\n"
        "2/ One click, your whole report as a spreadsheet.\n"
        "3/ Try it today."
    ),
    "linkedin": (
        "We just shipped CSV export.\n\n"
        "→ One click downloads your whole report.\n"
        "▸ Streams large datasets without timing out.\n\n"
        "Try it today."
    ),
    "blog": (
        "# Add CSV export\n\n"
        "We saw teams copy-pasting rows by hand. So we built a one-click "
        "spreadsheet export. Here is how it works and why it matters."
    ),
}


def test_social_copywriter_snapshot_structure_contract() -> None:
    """TC-22.5 (Slice 18): pinned social-copywriter stdout parses to the expected
    CopyBundle structure.

    Snapshots the OUTPUT STRUCTURE CONTRACT (not the agent file, which Slice 19
    installs): exactly the three CopyBundle keys, each a non-empty string, and
    the Twitter formatting rules — 3-8 numbered tweets each <= 280 chars, no
    Markdown `**bold**`, Unicode bold present.
    """
    parsed: dict[str, Any] = json.loads(json.dumps(_COPYWRITER_SNAPSHOT))

    # Parsed JSON shape — exactly the CopyBundle keys, nothing extra.
    assert set(parsed.keys()) == {"twitter_thread", "linkedin", "blog"}
    for value in parsed.values():
        assert isinstance(value, str) and value.strip()

    twitter: str = parsed["twitter_thread"]
    # No Markdown bold markers in the Twitter thread.
    assert "**" not in twitter
    # Unicode bold (Mathematical Bold block) IS present.
    assert any("\U0001d400" <= ch <= "\U0001d7ff" for ch in twitter)
    # 3-8 numbered tweets, each <= 280 chars.
    tweets = [line for line in twitter.splitlines() if line.strip()]
    assert 3 <= len(tweets) <= 8
    for tweet in tweets:
        assert len(tweet) <= 280


def test_social_copywriter_snapshot_rejects_markdown_bold() -> None:
    """A snapshot whose Twitter field uses Markdown `**bold**` violates contract."""
    bad = dict(_COPYWRITER_SNAPSHOT)
    bad["twitter_thread"] = "1/ **we** just shipped\n2/ try it\n3/ today"
    parsed: dict[str, Any] = json.loads(json.dumps(bad))
    assert "**" in parsed["twitter_thread"], "fixture must contain the violation"
    # The contract assertion the structure snapshot enforces would fail here.
    with pytest.raises(AssertionError):
        assert "**" not in parsed["twitter_thread"]


# --------------------------------------------------------------------------- #
# Agent file existence + frontmatter (TC-7.8 / TC-8.7 / AC-6.3)
# --------------------------------------------------------------------------- #


def _agent_file() -> Path:
    return Path.home() / ".claude" / "agents" / "brand-guardian.md"


def test_tc_7_8_brand_guardian_agent_file_exists_with_frontmatter() -> None:
    """TC-7.8: `~/.claude/agents/brand-guardian.md` exists with name/model/tools."""
    path = _agent_file()
    assert path.is_file(), f"agent file not installed at {path}"

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), "agent file must open with YAML frontmatter"
    # Extract the frontmatter block (between the first two '---' fences).
    _, fm, _body = text.split("---", 2)

    import yaml

    meta = yaml.safe_load(fm)
    assert isinstance(meta, dict)
    assert meta.get("name") == "brand-guardian"
    assert "model" in meta
    assert "tools" in meta
    assert isinstance(meta["tools"], list)


def test_tc_8_7_demo_script_writer_agent_file_exists_with_frontmatter() -> None:
    """TC-8.7: `~/.claude/agents/demo-script-writer.md` exists with name/model/tools."""
    path = Path.home() / ".claude" / "agents" / "demo-script-writer.md"
    assert path.is_file(), f"agent file not installed at {path}"

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), "agent file must open with YAML frontmatter"
    _, fm, _body = text.split("---", 2)

    import yaml

    meta = yaml.safe_load(fm)
    assert isinstance(meta, dict)
    assert meta.get("name") == "demo-script-writer"
    assert "model" in meta
    assert "tools" in meta
    assert isinstance(meta["tools"], list)
