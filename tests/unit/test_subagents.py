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
# social-copywriter (Slice 19, TC-13.8 / TC-22.5)
# --------------------------------------------------------------------------- #
#
# Slice 18 landed a forward-looking structure contract for the `social-copywriter`
# output. Slice 19 wires the real `s10_copy` stage + agent file, so this section
# now drives the pinned `claude -p` stdout through the REAL CopyStage parsing path
# (`_invoke_subagent`) and validates it against the REAL `CopyBundle` schema —
# still with NO real LLM call. The pinned LinkedIn/blog bodies are padded to the
# real 600-1200 / 1200-2000 word bounds the schema enforces.

import shipcast.stages.s10_copy as copy_mod  # noqa: E402
from shipcast.schemas import CopyBundle  # noqa: E402

# Unicode mathematical bold "just" (U+1D400 block), for the no-`**` assertion.
_UNI_BOLD = "\U0001d5f7\U0001d602\U0001d600\U0001d601"


def _pad_words(prefix: str, n_words: int) -> str:
    """Return `prefix` followed by enough filler words to reach `n_words` total."""
    have = len(prefix.split())
    return prefix + " " + " ".join(["value"] * max(0, n_words - have))


#: Pinned social-copywriter stdout: a conformant CopyBundle JSON object meeting
#: the REAL schema bounds. Twitter uses Unicode-bold (U+1D400 block), no `**`.
_COPYWRITER_SNAPSHOT: dict[str, Any] = {
    "twitter_thread": (
        f"1/ We {_UNI_BOLD} shipped CSV export.\n"
        "2/ One click, your whole report as a spreadsheet.\n"
        "3/ Try it today. If this helped, RT the first tweet."
    ),
    "linkedin": _pad_words(
        "We just shipped CSV export.\n\n"
        "→ One click downloads your whole report.\n"
        "▸ Streams large datasets without timing out.\n\n"
        "What would you automate with it?\n\n"
        "#ship #build #devtools #csv",
        700,
    ),
    "blog": _pad_words(
        "# Add CSV export\n\n**TL;DR**\n- one\n- two\n- three\n\n"
        "We saw teams copy-pasting rows by hand, so we built a one-click "
        "spreadsheet export. Try it. Feedback welcome. — the team",
        1300,
    ),
}


def test_social_copywriter_snapshot_parses_to_copy_bundle() -> None:
    """TC-13.8 / TC-22.5: pinned copywriter stdout parses to a valid CopyBundle.

    Drives the pinned stdout through the REAL `CopyStage._invoke_subagent` parsing
    path, then validates against the REAL `CopyBundle` schema: exactly the three
    keys, each non-empty, 3-8 numbered tweets each <= 280 chars, no `**`, Unicode
    bold present, LinkedIn/blog within the word bounds.
    """
    stage = copy_mod.CopyStage(
        subprocess_run=_stub_run(json.dumps(_COPYWRITER_SNAPSHOT))
    )

    parsed = stage._invoke_subagent("social-copywriter", "write the copy")

    # Parsed JSON shape — exactly the CopyBundle keys, nothing extra.
    assert set(parsed.keys()) == {"twitter_thread", "linkedin", "blog"}

    bundle = CopyBundle.model_validate(parsed)
    assert bundle.twitter_thread and bundle.linkedin and bundle.blog

    twitter = bundle.twitter_thread
    assert "**" not in twitter
    assert any("\U0001d400" <= ch <= "\U0001d7ff" for ch in twitter)
    tweets = [line for line in twitter.splitlines() if line.strip()]
    assert 3 <= len(tweets) <= 8
    for tweet in tweets:
        assert len(tweet) <= 280

    assert 600 <= len(bundle.linkedin.split()) <= 1200
    assert 1200 <= len(bundle.blog.split()) <= 2000


def test_social_copywriter_snapshot_rejects_markdown_bold() -> None:
    """A snapshot whose Twitter field uses Markdown `**bold**` fails CopyBundle."""
    from pydantic import ValidationError

    bad = dict(_COPYWRITER_SNAPSHOT)
    bad["twitter_thread"] = "1/ **we** just shipped\n2/ try it\n3/ today RT this"
    stage = copy_mod.CopyStage(subprocess_run=_stub_run(json.dumps(bad)))

    parsed = stage._invoke_subagent("social-copywriter", "write the copy")
    with pytest.raises(ValidationError):
        CopyBundle.model_validate(parsed)


def test_social_copywriter_snapshot_rejects_short_blog() -> None:
    """A snapshot whose blog is ~100 words fails CopyBundle's word-count bound."""
    from pydantic import ValidationError

    bad = dict(_COPYWRITER_SNAPSHOT)
    bad["blog"] = "word " * 100
    stage = copy_mod.CopyStage(subprocess_run=_stub_run(json.dumps(bad)))

    parsed = stage._invoke_subagent("social-copywriter", "write the copy")
    with pytest.raises(ValidationError):
        CopyBundle.model_validate(parsed)


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


def test_tc_13_7_social_copywriter_agent_file_exists_with_frontmatter() -> None:
    """TC-13.7: `~/.claude/agents/social-copywriter.md` exists with name/model/tools."""
    path = Path.home() / ".claude" / "agents" / "social-copywriter.md"
    assert path.is_file(), f"agent file not installed at {path}"

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), "agent file must open with YAML frontmatter"
    _, fm, _body = text.split("---", 2)

    import yaml

    meta = yaml.safe_load(fm)
    assert isinstance(meta, dict)
    assert meta.get("name") == "social-copywriter"
    assert meta.get("model") == "sonnet"
    assert "tools" in meta
    assert isinstance(meta["tools"], list)
    # Reports the uninstall command in the body (operator hygiene).
    assert "rm ~/.claude/agents/social-copywriter.md" in _body
