"""Reusable palette-conformance helper + vendored dE-CIE2000 correctness tests.

This module is BOTH a test module and the home of the reusable
:func:`assert_palette_conformance` helper that the graphics integration tests
(``tests/integration/test_s09_aspect_cards.py`` and later carousel/stat/code
tests) import and call on every generated PNG (Visual-style contract; testing
rule "Palette conformance test").

The helper implements the contract verbatim: PIL ``quantize(colors=5)`` the
image, then assert >= 80 % of pixels fall within dE-CIE2000 < 10 of one of the
five reference colours ``{primary, accent, neutral, #FFFFFF, #000000}``.

The colour maths is the project's own VENDORED dE-2000 (``shipcast.composition.color``)
- NOT ``colormath`` (Finding 7). The reference-pair tests below pin a subset of
the published CIEDE2000 data set (Sharma, Wu & Dalal 2005, Table 1) so a
regression in the vendored formula is caught immediately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shipcast.composition.color import (
    ciede2000,
    delta_e_hex,
    hex_to_rgb,
    srgb_to_lab,
)

# --------------------------------------------------------------------------- #
# Reusable palette-conformance helper (imported by the graphics tests)
# --------------------------------------------------------------------------- #

#: The two universal reference colours every on-brand graphic may also use
#: (text/background extremes) on top of the three brand hexes.
_WHITE: str = "#FFFFFF"
_BLACK: str = "#000000"

#: Conformance gate: fraction of quantized pixels that must be on-palette.
_MIN_ON_PALETTE_FRACTION: float = 0.80

#: dE-CIE2000 threshold under which a pixel counts as "matching" a reference.
_DELTA_E_THRESHOLD: float = 10.0


def assert_palette_conformance(
    png_path: Path,
    brand_colors: tuple[str, str, str],
    *,
    min_fraction: float = _MIN_ON_PALETTE_FRACTION,
    threshold: float = _DELTA_E_THRESHOLD,
) -> None:
    """Assert ``png_path`` conforms to the brand palette under dE-CIE2000.

    The image is PIL ``quantize(colors=5)``'d (the contract's 5-colour
    histogram), and each distinct quantized colour is measured (in dE-2000)
    against the five reference colours ``{primary, accent, neutral, white,
    black}``. A quantized colour "matches" when its nearest reference is within
    ``threshold``. The assertion passes iff the pixel-weighted fraction of
    matching colours is >= ``min_fraction``.

    Args:
        png_path: path to a PNG on disk.
        brand_colors: ``(primary, accent, neutral)`` brand hex strings.
        min_fraction: minimum on-palette pixel fraction (default 0.80).
        threshold: dE-2000 match threshold (default 10.0).

    Raises:
        AssertionError: when fewer than ``min_fraction`` of pixels are on-palette.
    """
    from PIL import Image

    primary, accent, neutral = brand_colors
    ref_labs = [
        srgb_to_lab(hex_to_rgb(h))
        for h in (primary, accent, neutral, _WHITE, _BLACK)
    ]

    with Image.open(png_path) as img:
        quantized = img.convert("RGB").quantize(colors=5)
        palette = quantized.getpalette() or []
        # color_index -> pixel count
        counts = quantized.getcolors(maxcolors=256) or []

    total = sum(count for count, _ in counts)
    assert total > 0, f"{png_path} has no pixels"

    on_palette = 0
    for count, index in counts:
        r = palette[index * 3 + 0]
        g = palette[index * 3 + 1]
        b = palette[index * 3 + 2]
        lab = srgb_to_lab((r, g, b))
        nearest = min(ciede2000(lab, ref) for ref in ref_labs)
        if nearest < threshold:
            on_palette += count

    fraction = on_palette / total
    assert fraction >= min_fraction, (
        f"{png_path}: only {fraction:.1%} of pixels within dE-2000<{threshold} of "
        f"the brand palette {brand_colors} (need >= {min_fraction:.0%})"
    )


# --------------------------------------------------------------------------- #
# TC-12.11 - helper passes on-palette / raises off-palette
# --------------------------------------------------------------------------- #

_BRAND = ("#FF6B6B", "#1D2A41", "#F4F1DE")


def _solid_png(path: Path, rgb: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> Path:
    from PIL import Image

    Image.new("RGB", size, rgb).save(path)
    return path


def test_helper_passes_for_on_palette_image(tmp_path: Path) -> None:
    """A solid swatch of the brand primary is fully on-palette."""
    png = _solid_png(tmp_path / "primary.png", hex_to_rgb(_BRAND[0]))
    assert_palette_conformance(png, _BRAND)


def test_helper_passes_for_white_image(tmp_path: Path) -> None:
    """Pure white is one of the two universal reference colours."""
    png = _solid_png(tmp_path / "white.png", (255, 255, 255))
    assert_palette_conformance(png, _BRAND)


def test_helper_raises_for_off_palette_image(tmp_path: Path) -> None:
    """A saturated lime that is far from every reference colour fails the gate."""
    # #00FF00 is dE-2000 far from a desaturated red/navy/cream palette + B/W.
    png = _solid_png(tmp_path / "lime.png", (0, 255, 0))
    with pytest.raises(AssertionError):
        assert_palette_conformance(png, _BRAND)


# --------------------------------------------------------------------------- #
# Vendored dE-2000 correctness vs. published CIEDE2000 reference pairs
# --------------------------------------------------------------------------- #

# A subset of the Sharma/Wu/Dalal (2005) CIEDE2000 reference data (Lab pairs and
# their published dE00 values, Table 1). These pairs deliberately exercise the
# formula's tricky branches (hue wrap-around, low-chroma, the R_T rotation term).
_CIEDE2000_REFERENCE: list[tuple[tuple[float, float, float], tuple[float, float, float], float]] = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


@pytest.mark.parametrize(("lab1", "lab2", "expected"), _CIEDE2000_REFERENCE)
def test_ciede2000_matches_reference(
    lab1: tuple[float, float, float],
    lab2: tuple[float, float, float],
    expected: float,
) -> None:
    """The vendored dE-2000 reproduces the published Sharma et al. values."""
    assert ciede2000(lab1, lab2) == pytest.approx(expected, abs=1e-4)


def test_ciede2000_is_zero_for_identical_colors() -> None:
    lab = srgb_to_lab(hex_to_rgb("#FF6B6B"))
    assert ciede2000(lab, lab) == pytest.approx(0.0, abs=1e-9)


def test_delta_e_hex_white_vs_black_is_large() -> None:
    """White and black are maximally far apart in lightness - dE well above 10."""
    assert delta_e_hex("#FFFFFF", "#000000") > 50.0


def test_hex_to_rgb_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        hex_to_rgb("#12")
