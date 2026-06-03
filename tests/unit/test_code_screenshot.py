"""Unit tests for the Pygments + PIL Ray.so-style code screenshot renderer.

Owned TCs (Section 12, Slice 18):
- TC-12.6 (component): ``render_code`` produces a real, openable PNG with a
  window chrome and syntax-highlighted, non-trivial pixel content. NO external
  API — Pygments tokenizes and PIL composites entirely locally.

The renderer is pure: zero network, zero subprocess, deterministic for a fixed
input. The tests assert the PNG decodes, has plausible dimensions, contains
more than one distinct colour (i.e. the syntax highlight actually rendered, not
a flat fill), and that the public ``extract_code_block`` helper pulls the first
fenced block out of changelog-style detail markdown.
"""

from __future__ import annotations

from pathlib import Path

from shipcast.marketing import code_screenshot

_SNIPPET = (
    "def add(a: int, b: int) -> int:\n"
    '    """Return the sum of two integers."""\n'
    "    return a + b\n"
)


def test_render_code_writes_openable_png(tmp_path: Path) -> None:
    """TC-12.6: render a Ray.so-style PNG that PIL can open at sane dims."""
    from PIL import Image

    out = tmp_path / "code.png"
    code_screenshot.render_code(
        _SNIPPET,
        language="python",
        palette=("#1D2A41", "#FF6B6B", "#F4F1DE"),
        out_path=out,
    )
    assert out.is_file()
    assert out.stat().st_size > 0
    with Image.open(out) as img:
        assert img.format == "PNG"
        w, h = img.size
        # A multi-line snippet must produce a frame larger than a single glyph.
        assert w >= 400
        assert h >= 120


def test_render_code_is_not_a_flat_fill(tmp_path: Path) -> None:
    """The rendered PNG must carry real syntax-highlight content.

    A flat-fill image (a bug where Pygments output was dropped) would collapse
    to one or two distinct colours. A real highlight + window chrome yields many.
    """
    from PIL import Image

    out = tmp_path / "code.png"
    code_screenshot.render_code(
        _SNIPPET,
        language="python",
        palette=("#1D2A41", "#FF6B6B", "#F4F1DE"),
        out_path=out,
    )
    with Image.open(out) as img:
        colors = img.convert("RGB").getcolors(maxcolors=1 << 16)
    assert colors is not None, "image has too many colours to count (still fine)"
    assert len(colors) > 8, "render looks like a flat fill — no highlight"


def test_render_code_is_deterministic(tmp_path: Path) -> None:
    """Same input → byte-identical PNG (pure, no timestamps in the body)."""
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    for out in (a, b):
        code_screenshot.render_code(
            _SNIPPET,
            language="python",
            palette=("#1D2A41", "#FF6B6B", "#F4F1DE"),
            out_path=out,
        )
    assert a.read_bytes() == b.read_bytes()


def test_render_code_unknown_language_falls_back(tmp_path: Path) -> None:
    """An unknown lexer name must not crash — it falls back to a text lexer."""
    from PIL import Image

    out = tmp_path / "code.png"
    code_screenshot.render_code(
        "just some plain text\nwith two lines\n",
        language="not-a-real-language",
        palette=("#1D2A41", "#FF6B6B", "#F4F1DE"),
        out_path=out,
    )
    with Image.open(out) as img:
        assert img.format == "PNG"


def test_extract_code_block_pulls_first_fenced_block() -> None:
    """The first ```` ``` ```` fenced block (with its info string) is returned."""
    details = (
        "We added a tiny helper.\n\n"
        "```python\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "```\n\n"
        "More prose after.\n"
    )
    code, language = code_screenshot.extract_code_block(details)
    assert "def add(a, b):" in code
    assert "return a + b" in code
    assert language == "python"
    # No prose leaked into the snippet.
    assert "More prose after." not in code


def test_extract_code_block_without_fence_returns_none() -> None:
    """Prose with no fenced block yields ``None`` so the caller can synthesize."""
    assert code_screenshot.extract_code_block("just prose, no code at all") is None


def test_extract_code_block_defaults_language_to_text() -> None:
    """A fence with no info string defaults the language to ``text``."""
    details = "```\nsome code\n```\n"
    result = code_screenshot.extract_code_block(details)
    assert result is not None
    code, language = result
    assert code.strip() == "some code"
    assert language == "text"
