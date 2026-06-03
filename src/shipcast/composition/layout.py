"""Pure PIL layout helpers - text outlining + 8-point-grid spacing.

Vendored and adapted from the upstream pipeline scaffold's outro-builder (the
``draw_outlined`` helper) plus the project's Visual-style contract (8-point
grid, >= 8 % padding on every static graphic).

These helpers are **pure** - no external API, no subprocess, no filesystem.
They are consumed by :mod:`shipcast.composition.captions` (Slice 15) and the
graphics stage (Slice 16). PIL is imported at module top; that is acceptable
because this module is only imported lazily - inside ``stage.run()`` - never at
``import shipcast.cli`` time, so CLI import-purity is preserved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import ImageDraw, ImageFont

#: The base grid unit (px). The Visual-style contract specifies an 8-point grid
#: for every static graphic; ``snap_to_grid`` rounds offsets to this unit.
GRID_UNIT: int = 8

#: Minimum padding fraction (>= 8 % of the smaller frame dimension) the
#: contract requires on every static graphic. ``min_padding`` derives the
#: pixel padding from a frame size using this fraction, snapped to the grid.
MIN_PADDING_FRACTION: float = 0.08


def snap_to_grid(value: float, unit: int = GRID_UNIT) -> int:
    """Round ``value`` to the nearest multiple of the grid ``unit``.

    Used to keep every text/chip offset on the 8-point grid so spacing reads
    consistent across cards and caption frames.

    Args:
        value: the raw pixel offset.
        unit: the grid unit (defaults to :data:`GRID_UNIT`).

    Returns:
        ``value`` snapped to the nearest multiple of ``unit``.

    Raises:
        ValueError: if ``unit`` is not positive.
    """
    if unit <= 0:
        raise ValueError(f"grid unit must be positive; got {unit}")
    return int(round(value / unit) * unit)


def min_padding(width: int, height: int, fraction: float = MIN_PADDING_FRACTION) -> int:
    """Return the minimum padding (px) for a ``width`` x ``height`` frame.

    Padding is ``fraction`` of the SMALLER frame dimension, snapped UP to the
    8-point grid so it never drops below the contractual minimum. A 1080-wide
    frame therefore yields >= 88 px (1080 x 0.08 = 86.4 -> snapped to 88).

    Args:
        width: frame width in px.
        height: frame height in px.
        fraction: padding fraction of the smaller dimension.

    Returns:
        Padding in px, snapped up to the nearest grid unit.

    Raises:
        ValueError: if ``width``/``height`` are not positive or ``fraction`` is
            not in ``(0, 0.5)``.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"frame dims must be positive; got {width}x{height}")
    if not 0.0 < fraction < 0.5:
        raise ValueError(f"padding fraction must be in (0, 0.5); got {fraction}")
    raw = min(width, height) * fraction
    # Snap UP so the padding is never below the contractual minimum.
    units = -(-int(raw) // GRID_UNIT)  # ceil division
    return units * GRID_UNIT


def draw_outlined(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[float, float],
    font: ImageFont.FreeTypeFont,
    fill: str,
    *,
    stroke_width: int = 4,
    stroke_fill: str = "#000000",
    anchor: str = "mt",
) -> None:
    """Draw ``text`` with a contrasting stroke so it reads on any background.

    Thin wrapper over ``ImageDraw.text`` that always supplies a stroke. Adapted
    verbatim (signature-modernized) from ``build_outro.py``'s ``draw_outlined``.

    Args:
        draw: the target ``ImageDraw.ImageDraw``.
        text: the string to render.
        xy: anchor coordinates.
        font: a loaded truetype font.
        fill: text colour (hex).
        stroke_width: outline thickness in px.
        stroke_fill: outline colour (hex).
        anchor: PIL text anchor (default ``"mt"`` - middle-top).
    """
    draw.text(
        xy,
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
        anchor=anchor,
    )
