"""Brand extraction — composes the Playwright client + PIL helpers (Slice 10).

Pure-ish utility layer that ``s03_brand`` calls. It does NOT construct clients
(the stage injects them) and does NOT write the manifest. Two responsibilities:

* :func:`extract_palette_and_font` — drive the (already-validated) live URL
  through the Playwright client to get the top-≤5 hex palette + body
  ``font-family``. SKIPPED by the stage when ``palette.hint.json`` is present.
* :func:`logo_png_bytes` — normalize the logo screenshot into PNG bytes. When
  the live app exposed no logo (``screenshot_logo`` returned ``None``), return a
  1x1 fully-transparent PNG and signal ``logo_detected=False`` (UC-4-A3).

The PIL import is lazy (inside :func:`transparent_1x1_png`) so importing this
module does not pull Pillow into ``sys.modules`` at CLI startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path


@runtime_checkable
class _PlaywrightLike(Protocol):
    """The minimal Playwright surface ``s03_brand`` drives (structural)."""

    def extract_css_palette(self, url: str) -> list[str]: ...
    def extract_font_family(self, url: str) -> str: ...
    def screenshot_logo(self, url: str) -> bytes | None: ...
    def screenshot_page(self, url: str) -> bytes: ...


@dataclass(frozen=True)
class PaletteFont:
    """Result of the live-app palette + font extraction."""

    palette: list[str]
    font_family: str


def extract_palette_and_font(playwright: _PlaywrightLike, url: str) -> PaletteFont:
    """Extract the top-≤5 hex palette and body font from the live app.

    The caller (``s03_brand.run``) is responsible for having validated ``url``
    via the SSRF defense BEFORE this is reached — the Playwright client also
    re-validates at the top of every navigating method (defense in depth), so a
    private/loopback URL raises before any browser navigation.
    """
    palette = playwright.extract_css_palette(url)
    font_family = playwright.extract_font_family(url)
    return PaletteFont(palette=palette, font_family=font_family)


#: Min ΔE-CIE2000 distance for ``accent`` to count as "distinct" from ``primary``.
_DISTINCT_DELTA_E: float = 12.0


def palette_from_image(png_bytes: bytes) -> list[str]:
    """Derive EXACTLY 3 distinct ``#RRGGBB`` hex codes from a screenshot.

    Returns ``[primary, accent, neutral]`` extracted from the real first-screen
    website screenshot (the policy ``s03_brand`` uses when a ``live_url`` is
    present and no ``palette.hint.json`` overrides it):

    * ``neutral`` — the MOST frequent colour (usually the page background).
    * ``primary`` — the most VIVID remaining colour (highest HSV saturation,
      then frequency).
    * ``accent``  — the next vivid colour that is sufficiently DISTINCT from
      ``primary`` (ΔE-CIE2000 ≥ :data:`_DISTINCT_DELTA_E`).

    If the image is too monochrome to yield three vivid, distinct colours, falls
    back to the top-3 colours by frequency (still guaranteed distinct). Pure and
    deterministic: identical bytes always yield the identical triple. The PIL
    import is lazy so importing this module never pulls Pillow into
    ``sys.modules``.
    """
    import colorsys
    import io

    from PIL import Image

    from shipcast.composition.color import delta_e_hex, hex_to_rgb

    with Image.open(io.BytesIO(png_bytes)) as raw:
        img = raw.convert("RGB")
        # Downscale so getcolors is cheap and pixel-frequency is stable; quantize
        # to a small palette so near-identical antialiased pixels collapse.
        img.thumbnail((256, 256))
        quant = img.quantize(colors=6).convert("RGB")

    raw_counts = quant.getcolors(maxcolors=256 * 256) or []
    counts: list[tuple[int, tuple[int, int, int]]] = []
    for count, rgb in raw_counts:
        # `quant` is RGB, so each colour is a 3-tuple; narrow for the type checker.
        assert isinstance(rgb, tuple)
        counts.append((int(count), (int(rgb[0]), int(rgb[1]), int(rgb[2]))))
    # Sort by descending frequency; tie-break on the RGB tuple for determinism.
    ranked = sorted(counts, key=lambda item: (-item[0], item[1]))
    ordered_rgb: list[tuple[int, int, int]] = [rgb for _count, rgb in ranked]

    def _hex(rgb: tuple[int, int, int]) -> str:
        return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"

    def _saturation(rgb: tuple[int, int, int]) -> float:
        _h, _l, s = colorsys.rgb_to_hls(
            rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0
        )
        return s

    # neutral = most frequent colour (page background).
    neutral_rgb = ordered_rgb[0]
    neutral = _hex(neutral_rgb)

    remaining = ordered_rgb[1:]
    # Rank the rest by vividness (saturation), tie-break on original frequency
    # order (already encoded as index in `remaining`).
    by_vividness = sorted(
        enumerate(remaining), key=lambda pair: (-_saturation(pair[1]), pair[0])
    )
    vivid_rgb = [rgb for _idx, rgb in by_vividness]

    primary = vivid_rgb[0] if vivid_rgb else None
    accent: tuple[int, int, int] | None = None
    if primary is not None:
        primary_hex = _hex(primary)
        for rgb in vivid_rgb[1:]:
            if delta_e_hex(primary_hex, _hex(rgb)) >= _DISTINCT_DELTA_E:
                accent = rgb
                break

    chosen: list[str] = []
    if primary is not None:
        chosen.append(_hex(primary))
    if accent is not None:
        chosen.append(_hex(accent))
    chosen.append(neutral)

    # Monochrome / too-few-vivid fallback: fill from the frequency ranking,
    # guaranteeing three DISTINCT hex codes.
    result: list[str] = []
    for hex_code in chosen + [_hex(rgb) for rgb in ordered_rgb]:
        if hex_code not in result:
            result.append(hex_code)
        if len(result) == 3:
            break

    # Last-resort distinctness guard (e.g. a single solid-colour image): pad with
    # deterministic near-shades of the last colour so we always return three.
    while len(result) < 3:
        base = hex_to_rgb(result[-1])
        shift = 8 * len(result)
        r, g, b = (min(255, c + shift) for c in base)
        candidate = f"#{r:02X}{g:02X}{b:02X}"
        if candidate in result:
            candidate = "#000000" if "#000000" not in result else "#FFFFFF"
        result.append(candidate)

    return result[:3]


@dataclass(frozen=True)
class LogoResult:
    """Logo bytes (real or 1x1 placeholder) plus whether a real logo was found."""

    png_bytes: bytes
    detected: bool


def logo_png_bytes(playwright: _PlaywrightLike, url: str) -> LogoResult:
    """Return the live app's logo PNG bytes, or a 1x1 transparent placeholder.

    ``screenshot_logo`` returns ``None`` when no logo selector matched on the
    page; in that case we write a 1x1 fully-transparent PNG and report
    ``detected=False`` so the operator knows to supply a replacement before
    approving (UC-4-A3 / TC-6.8).
    """
    raw = playwright.screenshot_logo(url)
    if raw is None:
        return LogoResult(png_bytes=transparent_1x1_png(), detected=False)
    return LogoResult(png_bytes=raw, detected=True)


def transparent_1x1_png() -> bytes:
    """Return the bytes of a 1x1 fully-transparent RGBA PNG.

    Built with Pillow (lazy import) so the output is a genuine, openable PNG with
    a valid ``\\x89PNG`` header rather than a hand-rolled byte blob.
    """
    import io

    from PIL import Image

    img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def write_png(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` (parent dirs created)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
