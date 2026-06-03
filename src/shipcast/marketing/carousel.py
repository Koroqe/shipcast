"""LinkedIn document-carousel composer — exactly 6 slides (Slice 18).

``s09_graphics`` always emits a 6-slide LinkedIn carousel at 1080x1350. The
mapping is fixed (CLAUDE.md "09_graphics"):

* slide 01 — the chosen LinkedIn **hook**
* slides 02-05 — the brief's FOUR ``carousel_beats``
* slide 06 — a **CTA**

This module owns ONLY the per-slide PIL composition. It is a **pure utility**
(``shipcast.marketing`` layer): no external API, no manifest, no subprocess. The
stage decides WHICH slide gets which text and calls :func:`render_slide` six
times; the composer never reads the brief.

* **Brand-consistent template.** A flat ``primary`` backdrop with the ``neutral``
  headline and an ``accent`` rule, all on the 8-point grid with >= 8 % padding
  (reuses :mod:`shipcast.composition.layout`). A small slide counter ("N / 6")
  anchors the bottom-right safe corner.
* **Lazy heavy imports.** PIL + the composition helpers import inside the
  function so importing this module — or ``shipcast.cli`` — does not pull PIL
  into ``sys.modules`` (import-purity invariant).
* **Deterministic.** Same inputs → byte-identical PNG.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

#: Canonical LinkedIn carousel slide size (portrait document format).
SLIDE_SIZE: tuple[int, int] = (1080, 1350)

#: The total slide count is FIXED at 6 (hook + 4 beats + CTA).
SLIDE_COUNT: int = 6

#: A slide's role, used only to vary the small label drawn above the headline.
SlideKind = Literal["hook", "beat", "cta"]

_KIND_LABEL: dict[str, str] = {
    "hook": "THE HOOK",
    "beat": "",
    "cta": "WHAT NOW",
}


def render_slide(
    idx: int,
    *,
    kind: SlideKind,
    headline: str,
    body: str,
    palette: tuple[str, str, str],
    font_path: Path | None,
    out_path: Path,
) -> None:
    """Render one carousel slide (1080x1350) to ``out_path``.

    The slide is a flat ``primary`` backdrop with the ``neutral`` headline
    vertically centred inside the >= 8 %-padded safe area, an ``accent`` rule
    under the headline, the optional ``body`` beneath it, and an ``accent`` slide
    counter ("``idx`` / 6") anchored bottom-right. All offsets snap to the
    8-point grid.

    Args:
        idx: 1-based slide number (1..6); rendered as "``idx`` / 6".
        kind: ``"hook"`` (slide 01), ``"beat"`` (slides 02-05), or ``"cta"``
            (slide 06) — varies only the small kicker label.
        headline: the slide headline (hook text / beat headline / CTA text).
        body: optional supporting line(s); empty for a headline-only slide.
        palette: ``(primary, accent, neutral)`` brand hex strings.
        font_path: brand display ``.ttf`` (or ``None`` for the system fallback).
        out_path: destination PNG path (parent dirs are created).
    """
    from PIL import Image, ImageDraw

    from shipcast.composition import layout
    from shipcast.composition.captions import _load_font

    width, height = SLIDE_SIZE
    primary, accent, neutral = palette

    image = Image.new("RGB", SLIDE_SIZE, primary)
    draw = ImageDraw.ImageDraw(image)
    pad = layout.min_padding(width, height)
    safe_w = width - 2 * pad

    headline_size = layout.snap_to_grid(width * 0.075)
    headline_font = _load_font(headline_size, font_path)
    body_size = layout.snap_to_grid(width * 0.040)
    body_font = _load_font(body_size, font_path)
    label_size = layout.snap_to_grid(width * 0.028)
    label_font = _load_font(label_size, font_path)

    headline_lines = _wrap(headline, headline_font, draw, safe_w)
    body_lines = _wrap(body, body_font, draw, safe_w) if body.strip() else []

    headline_lh = layout.snap_to_grid(headline_size * 1.25)
    body_lh = layout.snap_to_grid(body_size * 1.35)
    label = _KIND_LABEL.get(kind, "")
    label_lh = layout.snap_to_grid(label_size * 1.6) if label else 0
    rule_gap = layout.snap_to_grid(headline_size * 0.5)
    rule_h = layout.snap_to_grid(8)

    block_h = (
        label_lh
        + len(headline_lines) * headline_lh
        + rule_gap
        + rule_h
        + (layout.snap_to_grid(body_size) if body_lines else 0)
        + len(body_lines) * body_lh
    )
    top = layout.snap_to_grid((height - block_h) / 2)
    y = top

    if label:
        layout.draw_outlined(
            draw,
            label,
            (pad, y),
            label_font,
            fill=accent,
            stroke_fill=primary,
            stroke_width=2,
            anchor="lt",
        )
        y += label_lh

    for line in headline_lines:
        layout.draw_outlined(
            draw,
            line,
            (pad, y),
            headline_font,
            fill=neutral,
            stroke_fill=primary,
            anchor="lt",
        )
        y += headline_lh

    # Accent rule under the headline.
    y += rule_gap
    rule_w = layout.snap_to_grid(safe_w * 0.35)
    draw.rectangle((pad, y, pad + rule_w, y + rule_h), fill=accent)
    y += rule_h

    if body_lines:
        y += layout.snap_to_grid(body_size)
        for line in body_lines:
            layout.draw_outlined(
                draw,
                line,
                (pad, y),
                body_font,
                fill=neutral,
                stroke_fill=primary,
                anchor="lt",
            )
            y += body_lh

    # Slide counter anchored bottom-right inside the safe area.
    counter = f"{idx} / {SLIDE_COUNT}"
    layout.draw_outlined(
        draw,
        counter,
        (width - pad, height - pad),
        label_font,
        fill=accent,
        stroke_fill=primary,
        stroke_width=2,
        anchor="rb",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG")


def _wrap(
    text: str,
    font: object,
    draw: object,
    max_width: int,
) -> list[str]:
    """Greedy word-wrap so each rendered line fits within ``max_width`` px."""

    def line_width(s: str) -> float:
        bbox = draw.textbbox((0, 0), s, font=font)  # type: ignore[attr-defined]
        return float(bbox[2] - bbox[0])

    words = text.split()
    if not words:
        return [text] if text else []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if line_width(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines
