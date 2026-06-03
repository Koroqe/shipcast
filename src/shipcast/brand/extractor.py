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
