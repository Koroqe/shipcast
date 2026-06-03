"""Vendored pure-Python colour-difference primitives - sRGB -> CIE-Lab + CIEDE2000.

Architect Finding 7 (MINOR) mitigation. The Visual-style contract requires a
palette-conformance gate measured in dE-CIE2000 (dE-2000). The obvious library,
``colormath``, is unmaintained and calls the removed ``numpy.asscalar`` on modern
numpy, so importing it crashes. Rather than pin ancient transitive versions, we
vendor a small, dependency-free implementation here:

* :func:`srgb_to_lab` - 8-bit sRGB -> CIE-Lab (D65, 2° observer).
* :func:`ciede2000` - the full CIEDE2000 colour-difference formula
  (Sharma, Wu & Dalal 2005), the reference parametrization ``kL=kC=kH=1``.

The implementation is pure Python (``math`` only) - no numpy, no external API,
no I/O - so it is import-safe everywhere and can be unit-tested against the
published CIEDE2000 reference pairs. It is consumed by the graphics
palette-conformance helper (``tests/unit/test_palette_conformance.py``) and any
future on-brand colour check.
"""

from __future__ import annotations

import math

#: D65 reference white (X, Y, Z), 2° observer - the standard sRGB white point.
_D65_XN: float = 95.047
_D65_YN: float = 100.0
_D65_ZN: float = 108.883


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse a ``#rrggbb`` (or ``rrggbb``) hex string to an ``(r, g, b)`` triple.

    Args:
        value: a six-digit hex colour, optionally ``#``-prefixed; case-insensitive.

    Returns:
        The ``(r, g, b)`` channel values, each in ``0..255``.

    Raises:
        ValueError: if ``value`` is not a six-digit hex colour.
    """
    s = value.strip().lstrip("#")
    if len(s) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in s):
        raise ValueError(f"expected a '#rrggbb' hex colour, got {value!r}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _srgb_channel_to_linear(c: float) -> float:
    """Inverse sRGB companding for one 0..1 channel (gamma -> linear light)."""
    if c <= 0.04045:
        return c / 12.92
    return float(((c + 0.055) / 1.055) ** 2.4)


def srgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """Convert an 8-bit sRGB triple to CIE-Lab (D65, 2° observer).

    Pipeline: 8-bit -> 0..1 -> linear-light (inverse companding) -> CIE-XYZ
    (sRGB/D65 matrix) -> Lab. This is the standard colorimetric path; the
    resulting Lab values feed :func:`ciede2000`.

    Args:
        rgb: ``(r, g, b)`` channels, each ``0..255``.

    Returns:
        ``(L*, a*, b*)``.
    """
    r = _srgb_channel_to_linear(rgb[0] / 255.0)
    g = _srgb_channel_to_linear(rgb[1] / 255.0)
    b = _srgb_channel_to_linear(rgb[2] / 255.0)

    # Linear sRGB -> XYZ (D65), scaled to 0..100.
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) * 100.0
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) * 100.0
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) * 100.0

    def f(t: float) -> float:
        if t > 0.008856451679035631:  # (6/29)**3
            return float(t ** (1.0 / 3.0))
        return 7.787037037037037 * t + 16.0 / 116.0  # (1/3)*(29/6)**2 * t + 4/29

    fx = f(x / _D65_XN)
    fy = f(y / _D65_YN)
    fz = f(z / _D65_ZN)

    lightness = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b_star = 200.0 * (fy - fz)
    return lightness, a, b_star


def ciede2000(
    lab1: tuple[float, float, float],
    lab2: tuple[float, float, float],
) -> float:
    """Return the CIEDE2000 colour difference dE₀₀ between two CIE-Lab colours.

    Reference implementation of Sharma, Wu & Dalal (2005) with the standard
    parametric weighting factors ``kL = kC = kH = 1``. Validated against the
    published reference data set (see ``tests/unit/test_palette_conformance.py``).

    Args:
        lab1: first colour as ``(L*, a*, b*)``.
        lab2: second colour as ``(L*, a*, b*)``.

    Returns:
        The non-negative dE₀₀ distance.
    """
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2

    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0

    c_bar7 = c_bar**7
    g = 0.5 * (1.0 - math.sqrt(c_bar7 / (c_bar7 + 25.0**7)))

    a1p = (1.0 + g) * a1
    a2p = (1.0 + g) * a2
    c1p = math.hypot(a1p, b1)
    c2p = math.hypot(a2p, b2)

    h1p = _atan2_deg(b1, a1p)
    h2p = _atan2_deg(b2, a2p)

    delta_lp = l2 - l1
    delta_cp = c2p - c1p

    delta_hp = _delta_hp(c1p, c2p, h1p, h2p)
    delta_big_hp = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(delta_hp / 2.0))

    l_bar_p = (l1 + l2) / 2.0
    c_bar_p = (c1p + c2p) / 2.0
    h_bar_p = _h_bar_p(c1p, c2p, h1p, h2p)

    t = (
        1.0
        - 0.17 * math.cos(math.radians(h_bar_p - 30.0))
        + 0.24 * math.cos(math.radians(2.0 * h_bar_p))
        + 0.32 * math.cos(math.radians(3.0 * h_bar_p + 6.0))
        - 0.20 * math.cos(math.radians(4.0 * h_bar_p - 63.0))
    )

    delta_theta = 30.0 * math.exp(-(((h_bar_p - 275.0) / 25.0) ** 2))
    c_bar_p7 = c_bar_p**7
    r_c = 2.0 * math.sqrt(c_bar_p7 / (c_bar_p7 + 25.0**7))
    r_t = -math.sin(math.radians(2.0 * delta_theta)) * r_c

    sl = 1.0 + (0.015 * (l_bar_p - 50.0) ** 2) / math.sqrt(20.0 + (l_bar_p - 50.0) ** 2)
    sc = 1.0 + 0.045 * c_bar_p
    sh = 1.0 + 0.015 * c_bar_p * t

    term_l = delta_lp / sl
    term_c = delta_cp / sc
    term_h = delta_big_hp / sh

    return math.sqrt(
        term_l**2 + term_c**2 + term_h**2 + r_t * term_c * term_h
    )


def _atan2_deg(y: float, x: float) -> float:
    """``atan2`` in degrees, normalised to ``[0, 360)``; ``0`` when both are 0."""
    if x == 0.0 and y == 0.0:
        return 0.0
    deg = math.degrees(math.atan2(y, x))
    return deg + 360.0 if deg < 0.0 else deg


def _delta_hp(c1p: float, c2p: float, h1p: float, h2p: float) -> float:
    """Signed hue-angle difference ``h'2 - h'1`` per CIEDE2000 (degrees)."""
    if c1p * c2p == 0.0:
        return 0.0
    diff = h2p - h1p
    if diff > 180.0:
        return diff - 360.0
    if diff < -180.0:
        return diff + 360.0
    return diff


def _h_bar_p(c1p: float, c2p: float, h1p: float, h2p: float) -> float:
    """Mean hue angle ``h̄'`` per CIEDE2000 (degrees)."""
    if c1p * c2p == 0.0:
        return h1p + h2p
    diff = abs(h1p - h2p)
    summ = h1p + h2p
    if diff <= 180.0:
        return summ / 2.0
    if summ < 360.0:
        return (summ + 360.0) / 2.0
    return (summ - 360.0) / 2.0


def delta_e_hex(hex1: str, hex2: str) -> float:
    """Convenience: dE₀₀ between two ``#rrggbb`` hex colours."""
    return ciede2000(srgb_to_lab(hex_to_rgb(hex1)), srgb_to_lab(hex_to_rgb(hex2)))
