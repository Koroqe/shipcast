"""Pure Pygments + PIL Ray.so-style code screenshot renderer (Slice 18).

Produces an on-brand ``code.png`` for ``s09_graphics`` when the marketing brief
sets ``has_code_screenshot``. The render is a "Ray.so"-style card: a rounded
window frame with the three macOS traffic-light dots, a syntax-highlighted code
body, generous padding, and a soft drop shadow — all composited locally with
**zero external API calls**.

Architecture
------------
* **Pure utility** (``shipcast.marketing`` layer): no network, no subprocess, no
  manifest access. The stage passes the snippet + language + brand palette in.
* **Lazy heavy imports.** Pygments and PIL are imported INSIDE the functions, so
  importing this module — or ``shipcast.cli`` — does NOT pull either into
  ``sys.modules`` (import-purity invariant). Pygments is light, but the rule is
  uniform across the ``composition`` / ``marketing`` graphics utilities.
* **Deterministic.** No timestamps, no randomness in the rendered bytes — the
  same ``(code, language, palette)`` yields a byte-identical PNG.

The renderer never raises on an unknown language: it falls back to a plain-text
lexer so a misclassified snippet still produces a frame.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.ImageFont import FreeTypeFont

#: Window frame geometry (px). Padding inside the frame and the title-bar height.
_FRAME_PAD: int = 48
_TITLEBAR_H: int = 56
_LINE_PAD: int = 6
_FONT_SIZE: int = 28
#: Outer margin around the framed card (room for the drop shadow).
_OUTER_MARGIN: int = 56
_CORNER_RADIUS: int = 18
_SHADOW_OFFSET: int = 12
_SHADOW_BLUR: int = 18

#: Traffic-light dot colours + geometry.
_DOTS: tuple[str, str, str] = ("#FF5F56", "#FFBD2E", "#27C93F")
_DOT_RADIUS: int = 8
_DOT_GAP: int = 28

#: A fenced code block: optional info string, then body up to the closing fence.
_FENCE_RE = re.compile(
    r"```(?P<lang>[^\n`]*)\n(?P<body>.*?)\n?```",
    re.DOTALL,
)


def extract_code_block(text: str) -> tuple[str, str] | None:
    """Return ``(code, language)`` for the FIRST fenced block in ``text``.

    Parses changelog-style detail markdown for a ```` ``` ```` fenced block. The
    info string after the opening fence is the language (defaulting to ``"text"``
    when absent). Returns ``None`` when ``text`` carries no fenced block, so the
    caller can synthesize a representative snippet instead.

    Args:
        text: the markdown to scan (e.g. a changelog entry's ``details``).

    Returns:
        ``(code, language)`` for the first fence, or ``None`` if there is none.
    """
    match = _FENCE_RE.search(text)
    if match is None:
        return None
    lang = match.group("lang").strip() or "text"
    body = match.group("body")
    return body, lang


def _load_mono_font(size: int) -> FreeTypeFont:
    """Load a monospace truetype font, falling back to PIL's bitmap default.

    Tries a small set of common monospace faces present on macOS / Linux. A
    failure to load any scalable face falls back to PIL's bundled bitmap font so
    rendering never crashes.
    """
    from PIL import ImageFont

    candidates = (
        "Menlo.ttc",
        "/System/Library/Fonts/Menlo.ttc",
        "SFMono-Regular.otf",
        "DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "Courier New.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()  # type: ignore[return-value]


def render_code(
    code: str,
    *,
    language: str,
    palette: tuple[str, str, str],
    out_path: Path,
) -> None:
    """Render ``code`` as a Ray.so-style PNG card to ``out_path``.

    The snippet is tokenized by Pygments (a graceful fallback to a plain-text
    lexer when ``language`` is unknown), highlighted onto a dark window body with
    a title bar + three traffic-light dots, framed with rounded corners and a
    soft drop shadow on a ``neutral`` brand backdrop. Pure: no external API.

    Args:
        code: the source snippet (a few lines is ideal; long input is rendered
            verbatim but the card simply grows taller).
        language: a Pygments lexer name (``"python"``, ``"typescript"``, …);
            an unknown value falls back to a plain-text lexer.
        palette: ``(primary, accent, neutral)`` brand hex strings — ``primary``
            tints the window body, ``neutral`` is the card backdrop.
        out_path: destination PNG path (parent dirs are created).
    """
    from PIL import Image, ImageDraw, ImageFilter
    from pygments import lex  # type: ignore[import-untyped]
    from pygments.lexers import (  # type: ignore[import-untyped]
        get_lexer_by_name,
        guess_lexer,
    )
    from pygments.styles import get_style_by_name  # type: ignore[import-untyped]
    from pygments.token import Token  # type: ignore[import-untyped]
    from pygments.util import ClassNotFound  # type: ignore[import-untyped]

    from shipcast.composition.color import hex_to_rgb

    primary, _accent, neutral = palette

    # ----------------------------------------------------- tokenize (no crash)
    try:
        lexer = get_lexer_by_name(language)
    except ClassNotFound:
        try:
            lexer = guess_lexer(code)
        except ClassNotFound:
            from pygments.lexers.special import (  # type: ignore[import-untyped]
                TextLexer,
            )

            lexer = TextLexer()

    style = get_style_by_name("monokai")
    #: token-type → RGB colour, defaulting to a light foreground.
    default_fg = (248, 248, 242)

    def token_color(ttype: object) -> tuple[int, int, int]:
        tt = ttype
        while tt is not None:
            spec = style.styles.get(tt)
            if spec:
                hexpart = spec.split(" ")[-1] if spec else ""
                if hexpart.startswith("#") and len(hexpart) == 7:
                    return hex_to_rgb(hexpart)
            tt = getattr(tt, "parent", None)
        return default_fg

    # Group the flat token stream into physical lines, preserving colour runs.
    lines: list[list[tuple[str, tuple[int, int, int]]]] = [[]]
    for ttype, value in lex(code, lexer):
        if ttype in Token.Text and value == "\n":  # never reached; handled below
            pass
        parts = value.split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                lines.append([])
            if part:
                lines[-1].append((part, token_color(ttype)))
    if lines and not lines[-1]:
        lines.pop()
    if not lines:
        lines = [[(" ", default_fg)]]

    font = _load_mono_font(_FONT_SIZE)

    # ----------------------------------------------------- measure
    measure = Image.new("RGB", (1, 1))
    mdraw = ImageDraw.Draw(measure)
    char_w = int(mdraw.textlength("M", font=font)) or _FONT_SIZE // 2
    ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (
        _FONT_SIZE,
        _FONT_SIZE // 4,
    )
    line_h = ascent + descent + _LINE_PAD

    max_chars = max((sum(len(t) for t, _ in line) for line in lines), default=1)
    body_w = max_chars * char_w + 2 * _FRAME_PAD
    # Minimum window width so short snippets still look like a card.
    body_w = max(body_w, 520)
    body_h = _TITLEBAR_H + len(lines) * line_h + 2 * _FRAME_PAD

    canvas_w = body_w + 2 * _OUTER_MARGIN
    canvas_h = body_h + 2 * _OUTER_MARGIN

    # ----------------------------------------------------- backdrop + shadow
    backdrop = hex_to_rgb(neutral)
    canvas = Image.new("RGB", (canvas_w, canvas_h), backdrop)

    shadow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        (
            _OUTER_MARGIN + _SHADOW_OFFSET,
            _OUTER_MARGIN + _SHADOW_OFFSET,
            _OUTER_MARGIN + body_w + _SHADOW_OFFSET,
            _OUTER_MARGIN + body_h + _SHADOW_OFFSET,
        ),
        radius=_CORNER_RADIUS,
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(_SHADOW_BLUR))
    canvas.paste(shadow, (0, 0), shadow)

    # ----------------------------------------------------- window body
    # A dark body tinted toward the brand primary so the card reads on-brand.
    pr, pg, pb = hex_to_rgb(primary)
    body_bg = (max(pr - 30, 12), max(pg - 30, 12), max(pb - 30, 18))
    draw = ImageDraw.Draw(canvas)
    bx0, by0 = _OUTER_MARGIN, _OUTER_MARGIN
    bx1, by1 = _OUTER_MARGIN + body_w, _OUTER_MARGIN + body_h
    draw.rounded_rectangle(
        (bx0, by0, bx1, by1), radius=_CORNER_RADIUS, fill=body_bg
    )

    # Traffic-light dots in the title bar.
    dot_cy = by0 + _TITLEBAR_H // 2
    dot_cx = bx0 + _FRAME_PAD
    for dot_color in _DOTS:
        draw.ellipse(
            (
                dot_cx - _DOT_RADIUS,
                dot_cy - _DOT_RADIUS,
                dot_cx + _DOT_RADIUS,
                dot_cy + _DOT_RADIUS,
            ),
            fill=hex_to_rgb(dot_color),
        )
        dot_cx += _DOT_GAP

    # ----------------------------------------------------- code text
    text_x0 = bx0 + _FRAME_PAD
    text_y = by0 + _TITLEBAR_H + _FRAME_PAD // 2
    for line in lines:
        x = text_x0
        for run, color in line:
            draw.text((x, text_y), run, font=font, fill=color)
            x += int(mdraw.textlength(run, font=font))
        text_y += line_h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")
