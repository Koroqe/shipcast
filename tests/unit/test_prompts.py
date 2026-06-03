"""Unit tests for `shipcast.prompts.render_prompt`.

Covers all 8 statements in prompts.py:
- Successful render from a real or fake template
- TemplateNotFound propagates unchanged
- Variables are substituted in the rendered output
- `__all__` exports only `render_prompt`
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from jinja2 import TemplateNotFound

import shipcast.prompts as prompts_mod
from shipcast.prompts import render_prompt

# ---------------------------------------------------------------------------
# TC: module surface — only render_prompt exported
# ---------------------------------------------------------------------------


def test_all_exports_only_render_prompt() -> None:
    assert prompts_mod.__all__ == ["render_prompt"]


# ---------------------------------------------------------------------------
# TC: TemplateNotFound propagates unchanged for a missing template name
# ---------------------------------------------------------------------------


def test_template_not_found_propagates(tmp_path: Path) -> None:
    """render_prompt raises TemplateNotFound for a non-existent template."""
    with patch("shipcast.prompts.default_prompts_path", return_value=tmp_path):
        with pytest.raises(TemplateNotFound):
            render_prompt("does_not_exist.j2")


# ---------------------------------------------------------------------------
# TC: happy path — template variables substituted
# ---------------------------------------------------------------------------


def test_render_substitutes_variables(tmp_path: Path) -> None:
    template_file = tmp_path / "test.md.j2"
    template_file.write_text(
        "Hello {{ name }}! The score is {{ score }}.\n",
        encoding="utf-8",
    )
    with patch("shipcast.prompts.default_prompts_path", return_value=tmp_path):
        result = render_prompt("test.md.j2", name="Alice", score=42)

    assert "Hello Alice!" in result
    assert "42" in result


# ---------------------------------------------------------------------------
# TC: no autoescape — raw markdown/HTML is rendered verbatim
# ---------------------------------------------------------------------------


def test_no_autoescape_renders_markdown_verbatim(tmp_path: Path) -> None:
    template_file = tmp_path / "raw.j2"
    template_file.write_text("{{ content }}", encoding="utf-8")
    raw = "# Title\n**bold** & <em>italic</em>"
    with patch("shipcast.prompts.default_prompts_path", return_value=tmp_path):
        result = render_prompt("raw.j2", content=raw)
    # No HTML escaping should occur
    assert "<em>italic</em>" in result
    assert "&amp;" not in result


# ---------------------------------------------------------------------------
# TC: trailing newline preserved (keep_trailing_newline=True)
# ---------------------------------------------------------------------------


def test_trailing_newline_preserved(tmp_path: Path) -> None:
    template_file = tmp_path / "newline.j2"
    template_file.write_text("line\n", encoding="utf-8")
    with patch("shipcast.prompts.default_prompts_path", return_value=tmp_path):
        result = render_prompt("newline.j2")
    assert result.endswith("\n")


# ---------------------------------------------------------------------------
# TC: each call constructs a fresh Environment (stateless)
# ---------------------------------------------------------------------------


def test_each_call_is_independent(tmp_path: Path) -> None:
    t1 = tmp_path / "t1.j2"
    t2 = tmp_path / "t2.j2"
    t1.write_text("from-t1", encoding="utf-8")
    t2.write_text("from-t2", encoding="utf-8")
    with patch("shipcast.prompts.default_prompts_path", return_value=tmp_path):
        r1 = render_prompt("t1.j2")
        r2 = render_prompt("t2.j2")
    assert r1 == "from-t1"
    assert r2 == "from-t2"
