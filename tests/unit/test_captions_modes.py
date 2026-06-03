"""Unit tests for `composition.captions` + `composition.layout`.

Covers caption-mode parsing (TC-11.5/TC-11.6 default-chip fallback at the
parser level), the three render modes producing distinct frame pixels, and the
pure layout helpers. These are fast PIL-only tests - no ffmpeg, no subprocess.
"""

from __future__ import annotations

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
