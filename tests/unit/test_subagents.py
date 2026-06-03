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
# Agent file existence + frontmatter (TC-7.8 / AC-6.3)
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
