"""Brand-pack loader + validator (Slice 10).

Reads ``projects/_brand/<brand_slug>/`` and validates it against the brand-pack
contract (``.claude/CLAUDE.md`` → "Brand pack contract"). The contract REQUIRES:

* ``voice.md``  — tone / banned phrases / CTA pattern / ``caption_mode`` line.
* ``fonts/``    — at least one ``.ttf`` display font.
* ``logo.svg``  — or ``logo.png`` with transparency (either satisfies the logo
  requirement).

and treats these as OPTIONAL (their presence flips an extraction path off):

* ``palette.hint.json`` — pre-seeded ``{primary, accent, neutral}``; when present
  the Playwright palette extraction is SKIPPED entirely (faster, no SSRF surface).
* ``style_sheet.png``   — operator-supplied style sheet; when present the Gemini
  ``generate_image`` call is SKIPPED.

This module is a PURE leaf: it imports only stdlib + ``shipcast.errors`` and does
NO external API calls. ``validate`` raises :class:`BrandPackIncomplete` listing
EVERY missing required item BEFORE the stage touches any client, so an incomplete
pack never incurs cost or network I/O (FR-3.3 / TC-6.2..6.5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from shipcast.errors import BrandPackIncomplete

#: Directory under the projects root that holds every operator brand pack.
BRAND_PACK_DIRNAME = "_brand"

#: Canonical required-item labels surfaced in ``BrandPackIncomplete.missing``.
_VOICE_LABEL = "voice.md"
_FONTS_LABEL = "fonts/*.ttf"
_LOGO_LABEL = "logo.svg or logo.png"


@dataclass(frozen=True)
class BrandPack:
    """A validated, on-disk brand pack.

    All paths are absolute. ``palette_hint`` is the parsed
    ``palette.hint.json`` mapping (or ``None`` when absent), and
    ``style_sheet`` is the operator-supplied ``style_sheet.png`` (or ``None``).
    The presence of either flips the corresponding extraction path off in
    ``s03_brand.run`` (UC-4-A1 / UC-4-A2).
    """

    root: Path
    brand_slug: str
    voice_md: Path
    logo: Path
    fonts: tuple[Path, ...]
    palette_hint: dict[str, str] | None
    style_sheet: Path | None

    def input_paths(self) -> tuple[Path, ...]:
        """Every brand-pack file whose bytes should feed this stage's ``inputs_hash``.

        Returned to ``s03_brand.additional_input_paths`` so an operator edit to
        ANY brand-pack file invalidates the stage's recorded ``inputs_hash``
        (brand-drift coverage). Sorted for determinism.
        """
        paths: list[Path] = [self.voice_md, self.logo, *self.fonts]
        if self.style_sheet is not None:
            paths.append(self.style_sheet)
        # palette.hint.json is a real file when present — include it too.
        hint_path = self.root / "palette.hint.json"
        if hint_path.is_file():
            paths.append(hint_path)
        return tuple(sorted(paths, key=str))


def brand_pack_dir(projects_root: Path, brand_slug: str) -> Path:
    """Return ``<projects_root>/_brand/<brand_slug>/`` (not guaranteed to exist)."""
    return projects_root / BRAND_PACK_DIRNAME / brand_slug


def _find_logo(root: Path) -> Path | None:
    """Return the logo path (``logo.svg`` preferred, then ``logo.png``), or None."""
    svg = root / "logo.svg"
    if svg.is_file():
        return svg
    png = root / "logo.png"
    if png.is_file():
        return png
    return None


def _find_fonts(root: Path) -> tuple[Path, ...]:
    """Return every ``.ttf`` under ``fonts/`` (sorted), or an empty tuple."""
    fonts_dir = root / "fonts"
    if not fonts_dir.is_dir():
        return ()
    return tuple(sorted((p for p in fonts_dir.glob("*.ttf") if p.is_file()), key=str))


def _load_palette_hint(root: Path) -> dict[str, str] | None:
    """Parse ``palette.hint.json`` into ``{primary, accent, neutral}`` or None.

    A malformed hint (not an object, or missing one of the three keys) raises
    ``ValueError`` — the operator placed a broken file and should fix it rather
    than silently fall back to the (skipped) Playwright path.
    """
    hint_path = root / "palette.hint.json"
    if not hint_path.is_file():
        return None
    data = json.loads(hint_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(
            f"{hint_path} must contain a JSON object with primary/accent/neutral keys"
        )
    missing_keys = [k for k in ("primary", "accent", "neutral") if k not in data]
    if missing_keys:
        raise ValueError(
            f"{hint_path} is missing required key(s): {', '.join(missing_keys)}"
        )
    return {k: str(data[k]) for k in ("primary", "accent", "neutral")}


def validate(projects_root: Path, brand_slug: str) -> BrandPack:
    """Validate the brand pack for ``brand_slug`` and return a :class:`BrandPack`.

    Raises:
        BrandPackIncomplete: one or more REQUIRED files (``voice.md``, a
            ``fonts/*.ttf``, a logo) are missing. EVERY missing item is listed.
        ValueError: ``palette.hint.json`` is present but malformed.
    """
    root = brand_pack_dir(projects_root, brand_slug)

    voice_md = root / "voice.md"
    logo = _find_logo(root)
    fonts = _find_fonts(root)

    missing: list[str] = []
    if not voice_md.is_file():
        missing.append(_VOICE_LABEL)
    if not fonts:
        missing.append(_FONTS_LABEL)
    if logo is None:
        missing.append(_LOGO_LABEL)
    if missing:
        raise BrandPackIncomplete(brand_slug, tuple(missing))

    assert logo is not None  # narrowed by the missing-check above

    style_sheet = root / "style_sheet.png"
    return BrandPack(
        root=root,
        brand_slug=brand_slug,
        voice_md=voice_md,
        logo=logo,
        fonts=fonts,
        palette_hint=_load_palette_hint(root),
        style_sheet=style_sheet if style_sheet.is_file() else None,
    )
