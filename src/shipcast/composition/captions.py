"""PIL caption-frame renderer for the 1080x1920 showcase video.

Vendored and reframed from the upstream pipeline scaffold's subtitle-burn
renderer, then extended with two extra modes:

* ``chip``    - the original varied-tag-size chip strip; the currently-spoken
                word gets the ``active`` palette pair, the rest get ``inactive``.
                This is the channel default.
* ``karaoke`` - the chunk is rendered as a single inline word run; only the
                currently-spoken word is highlighted (active colour), the rest
                are dimmed - a continuous "karaoke" highlight rather than
                discrete chips.
* ``reveal``  - words appear progressively as they are spoken (a per-word
                fade-in / scale-up), so the chunk "reveals" word by word.

Brand pairing: the palette is built from the three approved brand hex codes
(``primary``, ``accent``, ``neutral``) so captions read on-brand. Homebrew
ffmpeg 8.x ships without libass/freetype, so - exactly as in the upstream
scaffold - we render one transparent RGBA PNG per video frame with PIL and
composite them via ffmpeg's ``overlay`` filter (the stage owns that ffmpeg
pass).

This module is **pure** (no external API, no subprocess). PIL is imported at
module top, which is fine because the module is only imported lazily inside
``s08_video.run()`` - never at ``import shipcast.cli`` time - preserving CLI
import-purity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict, cast

from PIL import Image, ImageDraw, ImageFont

from shipcast.composition import layout

#: Vertical showcase frame size. Every caption frame is exactly this size,
#: matching the Stage-06 clip geometry and the assembled ``showcase.mp4``.
FRAME_W: int = 1080
FRAME_H: int = 1920

#: Recognized caption modes. ``chip`` is the channel default (FR-14.8).
CaptionMode = Literal["chip", "karaoke", "reveal"]
_VALID_MODES: tuple[CaptionMode, ...] = ("chip", "karaoke", "reveal")
DEFAULT_MODE: CaptionMode = "chip"

#: Ordered system-font candidates tried when the brand display font is absent
#: or unreadable. The first that loads wins; if none load we fall back to PIL's
#: built-in bitmap font (which cannot scale but never crashes).
_FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


class WordDict(TypedDict):
    """One aligned word, the shape WhisperX emits in ``07_voice/words.json``."""

    word: str
    start_sec: float
    end_sec: float


class Palette(TypedDict):
    """A caption palette: ``(bg_hex, text_hex)`` pairs for active/inactive."""

    active: tuple[str, str]
    inactive: tuple[str, str]


def brand_palette(primary: str, accent: str, neutral: str) -> Palette:
    """Build a caption :class:`Palette` from the three approved brand hex codes.

    The currently-spoken word uses ``accent`` background with ``neutral`` text
    (the punchy highlight); the other words use ``primary`` background with
    ``neutral`` text. Pairing captions to the brand palette satisfies the
    Visual-style contract (captions read on-brand).

    Args:
        primary: brand primary hex (inactive chip background).
        accent: brand accent hex (active/highlight background).
        neutral: brand neutral hex (text colour on both).

    Returns:
        A :class:`Palette` mapping.
    """
    return {"active": (accent, neutral), "inactive": (primary, neutral)}


def parse_caption_mode(voice_md_text: str) -> CaptionMode:
    """Return the caption mode declared by a ``caption_mode:`` line, else ``chip``.

    The brand ``voice.md`` MAY contain a line ``caption_mode: <name>``. We scan
    case-insensitively and whitespace-tolerantly. An absent line OR an
    unrecognized value falls back to :data:`DEFAULT_MODE` (``chip``) WITHOUT
    raising (FR-14.8 / TC-11.5 / TC-11.6).

    Args:
        voice_md_text: the raw text of ``03_brand/voice.md``.

    Returns:
        One of ``"chip"``, ``"karaoke"``, ``"reveal"``.
    """
    for line in voice_md_text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("caption_mode:"):
            value = stripped.split(":", 1)[1].strip().lower()
            for mode in _VALID_MODES:
                if value == mode:
                    return mode
            return DEFAULT_MODE
    return DEFAULT_MODE


# --------------------------------------------------------------------------- #
# Font resolution (brand display font -> system candidates -> PIL default)
# --------------------------------------------------------------------------- #


def _load_font(size: int, font_path: Path | None) -> ImageFont.FreeTypeFont:
    """Load a scalable truetype font at ``size``, preferring the brand display font.

    Tries ``font_path`` first (the operator's ``03_brand`` display ``.ttf``),
    then each :data:`_FONT_CANDIDATES` entry. A stub/garbage ``.ttf`` (the test
    fixture ships a 21-byte placeholder) fails to parse and is skipped. If
    nothing scalable loads we return PIL's bundled bitmap font so rendering
    never crashes - captions still composite, just unscaled.
    """
    candidates: list[str] = []
    if font_path is not None:
        candidates.append(str(font_path))
    candidates.extend(_FONT_CANDIDATES)
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    # Last-ditch: the built-in bitmap font. Typed as FreeTypeFont for the
    # call-sites; ImageDraw treats both uniformly.
    return cast("ImageFont.FreeTypeFont", ImageFont.load_default())


def _size_for(word: str) -> int:
    """Pick a chip font size by word length (varied-tag look, carried over)."""
    length = len(word.strip())
    if length <= 3:
        return 72
    if length <= 5:
        return 88
    if length <= 7:
        return 104
    return 116


# --------------------------------------------------------------------------- #
# Word chunking (scaffold logic, unchanged)
# --------------------------------------------------------------------------- #


def chunk_words(
    words: list[WordDict],
    *,
    max_per_chunk: int = 4,
    pause_threshold: float = 0.4,
) -> list[list[WordDict]]:
    """Group ``words`` into caption chunks of <= ``max_per_chunk`` words.

    A new chunk also starts whenever the silent gap to the next word exceeds
    ``pause_threshold`` seconds, so chunk boundaries track natural phrasing.

    Args:
        words: aligned words (``{word, start_sec, end_sec}``).
        max_per_chunk: hard cap on words per chunk.
        pause_threshold: silence (s) that forces a chunk break.

    Returns:
        A list of chunks (each a list of word dicts).
    """
    chunks: list[list[WordDict]] = []
    cur: list[WordDict] = []
    for i, w in enumerate(words):
        cur.append(w)
        nxt_gap = (
            words[i + 1]["start_sec"] - w["end_sec"] if i + 1 < len(words) else 0.0
        )
        if len(cur) >= max_per_chunk or nxt_gap > pause_threshold:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _find_chunk(t: float, chunks: list[list[WordDict]]) -> list[WordDict] | None:
    """Return the chunk visible at time ``t`` (with a 0.1 s lead/tail), else None."""
    for chunk in chunks:
        if chunk[0]["start_sec"] - 0.1 <= t <= chunk[-1]["end_sec"] + 0.1:
            return chunk
    return None


# --------------------------------------------------------------------------- #
# Frame rendering
# --------------------------------------------------------------------------- #

#: Baseline distance from the bottom edge for the caption strip (px). Snapped
#: to the 8-pt grid and comfortably above the contractual >= 8 % bottom padding.
_BOTTOM_OFFSET: int = layout.snap_to_grid(FRAME_H * 0.18)
_PAD_X, _PAD_Y, _GAP = 28, 18, 16


def render_frame(
    t: float,
    chunks: list[list[WordDict]],
    palette: Palette,
    *,
    mode: CaptionMode = DEFAULT_MODE,
    font_path: Path | None = None,
) -> Image.Image:
    """Render ONE transparent 1080x1920 caption frame for playback time ``t``.

    Dispatches to the per-mode renderer. Outside any chunk's visible window the
    returned frame is fully transparent (no caption shown).

    Args:
        t: playback time in seconds.
        chunks: output of :func:`chunk_words`.
        palette: the brand-paired :class:`Palette`.
        mode: one of ``chip`` / ``karaoke`` / ``reveal``.
        font_path: optional brand display ``.ttf`` to prefer.

    Returns:
        An RGBA ``Image`` of size ``(FRAME_W, FRAME_H)``.

    Raises:
        ValueError: if ``mode`` is not a recognized caption mode.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown caption mode {mode!r}; expected one of {_VALID_MODES}")
    img = Image.new("RGBA", (FRAME_W, FRAME_H), (0, 0, 0, 0))
    chunk = _find_chunk(t, chunks)
    if not chunk:
        return img
    draw = ImageDraw.Draw(img)
    if mode == "chip":
        _render_chip(draw, t, chunk, palette, font_path)
    elif mode == "karaoke":
        _render_karaoke(draw, t, chunk, palette, font_path)
    else:  # reveal
        _render_reveal(img, draw, t, chunk, palette, font_path)
    return img


def _render_chip(
    draw: ImageDraw.ImageDraw,
    t: float,
    chunk: list[WordDict],
    palette: Palette,
    font_path: Path | None,
) -> None:
    """Varied-size rounded chips; the active word gets the highlight pair."""
    chips: list[tuple[str, ImageFont.FreeTypeFont, int, int, bool]] = []
    for w in chunk:
        text = w["word"].strip()
        if not text:
            continue
        font = _load_font(_size_for(text), font_path)
        text_w = draw.textlength(text, font=font)
        chip_w = int(text_w + _PAD_X * 2)
        chip_h = int(_size_for(text) + _PAD_Y * 2)
        active = w["start_sec"] <= t <= w["end_sec"]
        chips.append((text, font, chip_w, chip_h, active))
    if not chips:
        return
    total_w = sum(c[2] for c in chips) + _GAP * (len(chips) - 1)
    max_h = max(c[3] for c in chips)
    x = (FRAME_W - total_w) // 2
    y_top = FRAME_H - _BOTTOM_OFFSET
    for text, font, cw, ch, active in chips:
        chip_y = y_top + (max_h - ch) // 2
        bg, fg = palette["active"] if active else palette["inactive"]
        draw.rounded_rectangle([x, chip_y, x + cw, chip_y + ch], radius=24, fill=bg)
        draw.text((x + cw // 2, chip_y + ch // 2), text, font=font, fill=fg, anchor="mm")
        x += cw + _GAP


def _render_karaoke(
    draw: ImageDraw.ImageDraw,
    t: float,
    chunk: list[WordDict],
    palette: Palette,
    font_path: Path | None,
) -> None:
    """Single inline word run; only the spoken word is highlighted (active fg)."""
    font = _load_font(84, font_path)
    active_fg = palette["active"][0]
    inactive_fg = palette["inactive"][0]
    texts = [w["word"].strip() for w in chunk if w["word"].strip()]
    if not texts:
        return
    space_w = draw.textlength(" ", font=font)
    widths = [draw.textlength(tx, font=font) for tx in texts]
    total_w = sum(widths) + space_w * (len(texts) - 1)
    x = (FRAME_W - total_w) / 2
    y = FRAME_H - _BOTTOM_OFFSET
    visible = [w for w in chunk if w["word"].strip()]
    for w, tx, tw in zip(visible, texts, widths, strict=True):
        active = w["start_sec"] <= t <= w["end_sec"]
        fill = active_fg if active else inactive_fg
        layout.draw_outlined(
            draw, tx, (x, y), font, fill, stroke_width=5, anchor="lt"
        )
        x += tw + space_w


def _render_reveal(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    t: float,
    chunk: list[WordDict],
    palette: Palette,
    font_path: Path | None,
) -> None:
    """Words fade/scale in as they are spoken - progressive reveal."""
    font = _load_font(84, font_path)
    fg = palette["active"][0]
    space_w = draw.textlength(" ", font=font)
    spoken = [w for w in chunk if w["word"].strip() and w["start_sec"] <= t + 0.15]
    if not spoken:
        return
    texts = [w["word"].strip() for w in spoken]
    widths = [draw.textlength(tx, font=font) for tx in texts]
    total_w = sum(widths) + space_w * (len(texts) - 1)
    x = (FRAME_W - total_w) / 2
    y = FRAME_H - _BOTTOM_OFFSET
    for w, tx, tw in zip(spoken, texts, widths, strict=True):
        # Fade-in alpha over the word's first 0.2 s of life.
        age = max(0.0, t - w["start_sec"])
        alpha = int(min(1.0, age / 0.2) * 255) if w["start_sec"] <= t else 255
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ldraw = ImageDraw.Draw(layer)
        layout.draw_outlined(
            ldraw, tx, (x, y), font, fg, stroke_width=5, anchor="lt"
        )
        if alpha < 255:
            faded = layer.getchannel("A").point(lambda a, m=alpha: a * m // 255)
            layer.putalpha(faded)
        img.alpha_composite(layer)
        x += tw + space_w


# --------------------------------------------------------------------------- #
# Frame-sequence rendering (stage helper)
# --------------------------------------------------------------------------- #


def render_caption_frames(
    words: list[WordDict],
    *,
    total_frames: int,
    fps: int,
    palette: Palette,
    mode: CaptionMode,
    out_dir: Path,
    font_path: Path | None = None,
) -> int:
    """Render ``total_frames`` transparent caption PNGs into ``out_dir``.

    The stage feeds these into ffmpeg's ``overlay`` filter. Returns the number
    of frames written (== ``total_frames``). Filenames are ``f_NNNNN.png`` so a
    single ``-i f_%05d.png`` glob picks them up in order.

    Args:
        words: aligned words from ``07_voice/words.json``.
        total_frames: number of frames to render (video duration x fps + 1).
        fps: target framerate (matches the assembled video).
        palette: brand-paired :class:`Palette`.
        mode: caption mode.
        out_dir: directory the PNGs are written to (created if absent).
        font_path: optional brand display ``.ttf``.

    Returns:
        The count of frames written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = chunk_words(words)
    for i in range(total_frames):
        t = i / fps
        frame = render_frame(t, chunks, palette, mode=mode, font_path=font_path)
        frame.save(out_dir / f"f_{i:05d}.png", format="PNG")
    return total_frames
