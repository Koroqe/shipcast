"""Unit tests for `composition.captions` + `composition.layout`.

Covers caption-mode parsing (TC-11.5/TC-11.6 default-chip fallback at the
parser level), the three render modes producing distinct frame pixels, and the
pure layout helpers. These are fast PIL-only tests - no ffmpeg, no subprocess.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from shipcast.composition import captions, layout

# --------------------------------------------------------------------------- #
# layout helpers
# --------------------------------------------------------------------------- #


def test_snap_to_grid_rounds_to_nearest_unit() -> None:
    assert layout.snap_to_grid(0) == 0
    assert layout.snap_to_grid(5) == 8
    assert layout.snap_to_grid(11) == 8
    assert layout.snap_to_grid(13) == 16


def test_min_padding_is_at_least_8_percent_and_grid_snapped() -> None:
    pad = layout.min_padding(1080, 1920)
    assert pad >= 1080 * layout.MIN_PADDING_FRACTION
    assert pad % layout.GRID_UNIT == 0


def test_min_padding_rejects_bad_dims() -> None:
    import pytest

    with pytest.raises(ValueError):
        layout.min_padding(0, 100)
    with pytest.raises(ValueError):
        layout.min_padding(100, 100, fraction=0.9)


# --------------------------------------------------------------------------- #
# caption-mode parsing
# --------------------------------------------------------------------------- #


def test_parse_caption_mode_reads_explicit_line() -> None:
    assert captions.parse_caption_mode("caption_mode: chip") == "chip"
    assert captions.parse_caption_mode("foo\ncaption_mode: karaoke\nbar") == "karaoke"
    assert captions.parse_caption_mode("caption_mode: reveal") == "reveal"


def test_parse_caption_mode_absent_defaults_chip() -> None:
    assert captions.parse_caption_mode("no mode line here") == "chip"
    assert captions.parse_caption_mode("") == "chip"


def test_parse_caption_mode_unrecognized_defaults_chip() -> None:
    assert captions.parse_caption_mode("caption_mode: fancytype") == "chip"


def test_parse_caption_mode_case_and_whitespace_tolerant() -> None:
    assert captions.parse_caption_mode("  CAPTION_MODE:   Karaoke  ") == "karaoke"


# --------------------------------------------------------------------------- #
# frame rendering - all three modes produce captioned pixels
# --------------------------------------------------------------------------- #

_WORDS = [
    {"word": "Ship", "start_sec": 0.0, "end_sec": 0.4},
    {"word": "faster", "start_sec": 0.4, "end_sec": 0.9},
    {"word": "today", "start_sec": 0.9, "end_sec": 1.4},
]
_PALETTE = captions.brand_palette("#FF6B6B", "#1D2A41", "#F4F1DE")


def _nonblank_pixel_count(img: Image.Image) -> int:
    """Count pixels with non-zero alpha via the alpha-channel histogram."""
    alpha = img.convert("RGBA").getchannel("A")
    # histogram()[0] is the count of fully-transparent pixels.
    return alpha.width * alpha.height - alpha.histogram()[0]


def test_render_frame_chip_draws_pixels_during_speech() -> None:
    chunks = captions.chunk_words(_WORDS, max_per_chunk=4, pause_threshold=0.4)
    frame = captions.render_frame(0.5, chunks, _PALETTE, mode="chip")
    assert frame.size == (captions.FRAME_W, captions.FRAME_H)
    assert _nonblank_pixel_count(frame) > 0


def test_render_frame_blank_outside_speech() -> None:
    chunks = captions.chunk_words(_WORDS, max_per_chunk=4, pause_threshold=0.4)
    frame = captions.render_frame(99.0, chunks, _PALETTE, mode="chip")
    assert _nonblank_pixel_count(frame) == 0


def test_all_three_modes_render_distinct_pixels() -> None:
    chunks = captions.chunk_words(_WORDS, max_per_chunk=4, pause_threshold=0.4)
    chip = captions.render_frame(0.5, chunks, _PALETTE, mode="chip")
    karaoke = captions.render_frame(0.5, chunks, _PALETTE, mode="karaoke")
    reveal = captions.render_frame(0.5, chunks, _PALETTE, mode="reveal")
    for f in (chip, karaoke, reveal):
        assert _nonblank_pixel_count(f) > 0
    # Karaoke highlights the active word differently from chip's uniform chips,
    # so at least one mode pair differs in raw bytes.
    assert chip.tobytes() != karaoke.tobytes() or chip.tobytes() != reveal.tobytes()


def test_invalid_mode_rejected() -> None:
    import pytest

    chunks = captions.chunk_words(_WORDS, max_per_chunk=4, pause_threshold=0.4)
    with pytest.raises(ValueError):
        captions.render_frame(0.5, chunks, _PALETTE, mode="bogus")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# caption OVERFLOW regression - long chunks must wrap and never exceed the
# safe horizontal band [_SIDE_MARGIN, FRAME_W - _SIDE_MARGIN].
# --------------------------------------------------------------------------- #

# A real scalable font so the test exercises genuine sizing (the repo fixture
# font is an unscalable bitmap fallback that would not reproduce overflow).
_REAL_FONTS = (
    "/Library/Fonts/SpaceGrotesk-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
_FONT_PATH = next((Path(p) for p in _REAL_FONTS if os.path.exists(p)), None)

# Four long words at the largest chip sizes - this is the real-run overflow case.
_LONG_WORDS: list[captions.WordDict] = [
    {"word": "internationalization", "start_sec": 0.0, "end_sec": 0.5},
    {"word": "responsibilities", "start_sec": 0.5, "end_sec": 1.0},
    {"word": "infrastructure", "start_sec": 1.0, "end_sec": 1.5},
    {"word": "authentication", "start_sec": 1.5, "end_sec": 2.0},
]


def _opaque_column_mask(img: Image.Image) -> list[bool]:
    """Per-column flag: True if that column has any non-transparent pixel."""
    alpha = img.convert("RGBA").getchannel("A")
    width, height = alpha.size
    px = alpha.load()
    assert px is not None
    cols = [False] * width
    for x in range(width):
        for y in range(height):
            if px[x, y] != 0:
                cols[x] = True
                break
    return cols


def _opaque_row_bands(img: Image.Image) -> int:
    """Count contiguous vertical bands of rows that contain opaque pixels."""
    alpha = img.convert("RGBA").getchannel("A")
    width, height = alpha.size
    px = alpha.load()
    assert px is not None
    bands = 0
    in_band = False
    for y in range(height):
        row_has = any(px[x, y] != 0 for x in range(width))
        if row_has and not in_band:
            bands += 1
            in_band = True
        elif not row_has:
            in_band = False
    return bands


@pytest.mark.parametrize("mode", ["chip", "karaoke", "reveal"])
def test_long_chunk_never_overflows_safe_band(mode: captions.CaptionMode) -> None:
    """Every opaque pixel column must lie within the safe horizontal band."""
    chunks = captions.chunk_words(
        _LONG_WORDS, max_per_chunk=4, pause_threshold=10.0
    )
    assert len(chunks) == 1  # one chunk of four long words
    # Render at a time the last word is active so all words are visible
    # (reveal needs every word to have started).
    frame = captions.render_frame(
        1.9, chunks, _PALETTE, mode=mode, font_path=_FONT_PATH
    )
    cols = _opaque_column_mask(frame)
    left_margin = range(0, captions._SIDE_MARGIN)
    right_margin = range(captions.FRAME_W - captions._SIDE_MARGIN, captions.FRAME_W)
    assert not any(cols[x] for x in left_margin), "opaque pixels in LEFT margin"
    assert not any(cols[x] for x in right_margin), "opaque pixels in RIGHT margin"
    # And something WAS actually drawn.
    assert any(cols)


@pytest.mark.parametrize("mode", ["chip", "karaoke", "reveal"])
def test_long_chunk_actually_wraps_to_multiple_rows(
    mode: captions.CaptionMode,
) -> None:
    """The long chunk must wrap into more than one stacked row/line."""
    chunks = captions.chunk_words(
        _LONG_WORDS, max_per_chunk=4, pause_threshold=10.0
    )
    frame = captions.render_frame(
        1.9, chunks, _PALETTE, mode=mode, font_path=_FONT_PATH
    )
    assert _opaque_row_bands(frame) > 1, "expected wrapping into multiple rows"


def test_wrap_helper_greedily_packs_within_usable_width() -> None:
    """_wrap packs items into rows whose summed width + gaps fit usable_w."""
    items = [("a", 400.0), ("b", 400.0), ("c", 400.0), ("d", 200.0)]
    rows = captions._wrap(items, usable_w=900.0, gap=16.0)
    # 400+16+400 = 816 <= 900 fits two; third would be 816+16+400 > 900.
    assert rows == [["a", "b"], ["c", "d"]]


def test_wrap_helper_places_oversize_item_alone() -> None:
    """A single item wider than usable_w still gets its own row (no drop)."""
    items = [("big", 2000.0), ("x", 100.0)]
    rows = captions._wrap(items, usable_w=900.0, gap=16.0)
    assert rows == [["big"], ["x"]]


def test_over_wide_single_word_is_shrunk_to_fit() -> None:
    """A single very long word at a big font is shrunk so it never overflows."""
    if _FONT_PATH is None:  # pragma: no cover - real font present on dev machines
        pytest.skip("no scalable system font available")
    huge: list[captions.WordDict] = [
        {
            "word": "supercalifragilisticexpialidocious",
            "start_sec": 0.0,
            "end_sec": 1.0,
        },
    ]
    chunks = captions.chunk_words(huge, max_per_chunk=4, pause_threshold=10.0)
    for mode in ("chip", "karaoke", "reveal"):
        frame = captions.render_frame(
            0.5, chunks, _PALETTE, mode=mode, font_path=_FONT_PATH  # type: ignore[arg-type]
        )
        cols = _opaque_column_mask(frame)
        assert not any(cols[x] for x in range(0, captions._SIDE_MARGIN))
        assert not any(
            cols[x]
            for x in range(captions.FRAME_W - captions._SIDE_MARGIN, captions.FRAME_W)
        )
        assert any(cols)
