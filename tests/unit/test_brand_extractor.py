"""Unit tests for the brand layer (Slice 10): loader + extractor.

Covers the pure pieces ``s03_brand`` composes:
- ``brand.loader.validate`` — BrandPackIncomplete listing (TC-6.2..6.5),
  palette.hint parsing (TC-6.6 enabling), input_paths drift coverage.
- ``brand.extractor`` — palette/font extraction, logo None → 1x1 transparent
  PNG (TC-6.8), placeholder PNG validity.

No external API / network: the Playwright surface is a hand-rolled fake.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipcast.brand import extractor, loader
from shipcast.errors import BrandPackIncomplete

_FIXTURE_PACK = Path(__file__).resolve().parent.parent / "fixtures" / "brand" / "test-brand"


def _seed_pack(
    projects_root: Path,
    *,
    slug: str = "test-brand",
    voice: bool = True,
    fonts: bool = True,
    logo: str | None = "svg",
    palette_hint: dict[str, str] | None = None,
    style_sheet: bool = False,
) -> Path:
    """Create a brand pack under ``<projects_root>/_brand/<slug>/``."""
    root = projects_root / "_brand" / slug
    (root / "fonts").mkdir(parents=True, exist_ok=True)
    if voice:
        (root / "voice.md").write_text("caption_mode: chip\n", encoding="utf-8")
    if fonts:
        (root / "fonts" / "Inter.ttf").write_bytes(b"TTF")
    if logo == "svg":
        (root / "logo.svg").write_text("<svg/>", encoding="utf-8")
    elif logo == "png":
        (root / "logo.png").write_bytes(extractor.transparent_1x1_png())
    if palette_hint is not None:
        (root / "palette.hint.json").write_text(
            json.dumps(palette_hint), encoding="utf-8"
        )
    if style_sheet:
        (root / "style_sheet.png").write_bytes(extractor.transparent_1x1_png())
    return root


# --------------------------------------------------------------------------- #
# loader.validate — completeness
# --------------------------------------------------------------------------- #


def test_validate_complete_pack(tmp_path: Path) -> None:
    _seed_pack(tmp_path)
    pack = loader.validate(tmp_path, "test-brand")
    assert pack.voice_md.name == "voice.md"
    assert pack.logo.name == "logo.svg"
    assert len(pack.fonts) == 1
    assert pack.palette_hint is None
    assert pack.style_sheet is None


def test_missing_voice_md_raises_listing_it(tmp_path: Path) -> None:
    """TC-6.2: missing voice.md → BrandPackIncomplete listing voice.md."""
    _seed_pack(tmp_path, voice=False)
    with pytest.raises(BrandPackIncomplete) as exc:
        loader.validate(tmp_path, "test-brand")
    assert "voice.md" in str(exc.value)
    assert any("voice.md" in m for m in exc.value.missing)


def test_missing_fonts_raises(tmp_path: Path) -> None:
    """TC-6.3: empty fonts/ → BrandPackIncomplete listing the font."""
    _seed_pack(tmp_path, fonts=False)
    with pytest.raises(BrandPackIncomplete) as exc:
        loader.validate(tmp_path, "test-brand")
    assert any("ttf" in m.lower() for m in exc.value.missing)


def test_missing_logo_raises(tmp_path: Path) -> None:
    """TC-6.4: neither logo.svg nor logo.png → BrandPackIncomplete."""
    _seed_pack(tmp_path, logo=None)
    with pytest.raises(BrandPackIncomplete) as exc:
        loader.validate(tmp_path, "test-brand")
    assert any("logo" in m.lower() for m in exc.value.missing)


def test_all_three_missing_listed(tmp_path: Path) -> None:
    """TC-6.5: empty pack → all three required items in the message."""
    (tmp_path / "_brand" / "test-brand").mkdir(parents=True)
    with pytest.raises(BrandPackIncomplete) as exc:
        loader.validate(tmp_path, "test-brand")
    assert len(exc.value.missing) == 3
    msg = str(exc.value)
    assert "voice.md" in msg
    assert "ttf" in msg.lower()
    assert "logo" in msg.lower()


def test_png_logo_accepted(tmp_path: Path) -> None:
    _seed_pack(tmp_path, logo="png")
    pack = loader.validate(tmp_path, "test-brand")
    assert pack.logo.name == "logo.png"


# --------------------------------------------------------------------------- #
# loader.validate — palette hint + style sheet detection
# --------------------------------------------------------------------------- #


def test_palette_hint_parsed(tmp_path: Path) -> None:
    hint = {"primary": "#FF0000", "accent": "#00FF00", "neutral": "#0000FF"}
    _seed_pack(tmp_path, palette_hint=hint)
    pack = loader.validate(tmp_path, "test-brand")
    assert pack.palette_hint == hint


def test_malformed_palette_hint_missing_key_raises(tmp_path: Path) -> None:
    _seed_pack(tmp_path, palette_hint={"primary": "#FF0000"})
    with pytest.raises(ValueError, match="missing required key"):
        loader.validate(tmp_path, "test-brand")


def test_style_sheet_detected(tmp_path: Path) -> None:
    _seed_pack(tmp_path, style_sheet=True)
    pack = loader.validate(tmp_path, "test-brand")
    assert pack.style_sheet is not None
    assert pack.style_sheet.name == "style_sheet.png"


def test_input_paths_includes_every_pack_file(tmp_path: Path) -> None:
    hint = {"primary": "#FF0000", "accent": "#00FF00", "neutral": "#0000FF"}
    _seed_pack(tmp_path, palette_hint=hint, style_sheet=True)
    pack = loader.validate(tmp_path, "test-brand")
    names = {p.name for p in pack.input_paths()}
    assert {"voice.md", "logo.svg", "Inter.ttf", "palette.hint.json", "style_sheet.png"} <= names


# --------------------------------------------------------------------------- #
# extractor
# --------------------------------------------------------------------------- #


class _FakePlaywright:
    def __init__(self, *, logo: bytes | None) -> None:
        self._logo = logo

    def extract_css_palette(self, url: str) -> list[str]:
        return ["#112233", "#445566", "#778899"]

    def extract_font_family(self, url: str) -> str:
        return "Inter, sans-serif"

    def screenshot_logo(self, url: str) -> bytes | None:
        return self._logo


def test_extract_palette_and_font() -> None:
    pw = _FakePlaywright(logo=b"")
    result = extractor.extract_palette_and_font(pw, "https://example.com")
    assert result.palette == ["#112233", "#445566", "#778899"]
    assert result.font_family == "Inter, sans-serif"


def test_logo_present_returns_detected_true() -> None:
    pw = _FakePlaywright(logo=b"\x89PNG\r\n\x1a\nrest")
    result = extractor.logo_png_bytes(pw, "https://example.com")
    assert result.detected is True
    assert result.png_bytes.startswith(b"\x89PNG")


def test_logo_none_returns_transparent_placeholder() -> None:
    """TC-6.8: screenshot_logo None → 1x1 transparent PNG, detected False."""
    pw = _FakePlaywright(logo=None)
    result = extractor.logo_png_bytes(pw, "https://example.com")
    assert result.detected is False
    assert result.png_bytes.startswith(b"\x89PNG")
    # It is a real, openable 1x1 image.
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(result.png_bytes))
    assert img.size == (1, 1)
    assert img.mode == "RGBA"
    assert img.getpixel((0, 0)) == (0, 0, 0, 0)


def test_transparent_1x1_png_is_valid() -> None:
    data = extractor.transparent_1x1_png()
    assert data.startswith(b"\x89PNG")
