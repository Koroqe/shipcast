"""Stage 09 - static graphics.

Turns the approved marketing brief (``04_plan/brief.json``) and brand pack
(``03_brand/``) into the static graphics package under ``09_graphics/``:

* **4 aspect-ratio cards** (Slice 16): ``1x1.png`` (1080x1080), ``16x9.png``
  (1920x1080), ``9x16.png`` (1080x1920), ``4x5.png`` (1080x1350). Each is a
  Gemini Imagen background at that ratio with the entry headline overlaid via
  PIL ``draw_outlined`` in the brand display font.
* **OG card** (Slice 17): ``og_card.png`` (1200x630) with entry name + logo.
* **stat card** (Slice 17, conditional on ``brief.has_stat_card``): ``stat_*.png``.
* **code screenshot** (Slice 18, conditional on ``brief.has_code_screenshot``):
  ``code.png`` (Pygments + PIL - no external API).
* **LinkedIn carousel** (Slice 18): ``carousel/slide_01.png`` ... ``slide_06.png``
  (each 1080x1350).

Slice scoping
-------------
This module ships the FULL ``run()`` shell that dispatches to
``_render_aspect_card`` / ``_render_og`` / ``_render_stat`` / ``_render_code`` /
``_render_carousel_slide``. Slice 16 landed ``_render_aspect_card`` + the shell;
Slice 17 landed ``_render_og`` + ``_render_stat``; **Slice 18 landed
``_render_code`` (conditional, Pygments + PIL) + ``_render_carousel_slide``
(always, 6-slide LinkedIn carousel via the pure ``marketing.carousel``
composer)**. All five renderers are now wired into ``run()``.

Architecture
------------
* Pure stage - the dispatcher owns manifest writes, locking, and the human gate.
* The Gemini Imagen call lives in THIS stage, never pushed into ``marketing/``
  (architect Module-Boundary Risk 2). The retry loop on transient Imagen errors
  lives here (architect Ruling 7); a :class:`GeminiSafetyBlocked` is re-raised
  unwrapped so the dispatcher records ``error.type == "GeminiSafetyBlocked"``.
* PIL + the composition helpers are imported lazily inside ``run()`` /
  ``_render_aspect_card`` so importing this module - or ``shipcast.cli`` - does
  NOT pull PIL into ``sys.modules`` (import-purity invariant).

Cost
----
Imagen is a PAID call. Cost accrues only into the returned
``StageResult.metrics["cost_usd"]``; the dispatcher writes it on the DONE
transition. ``next_call_cost_usd`` returns the single-most-expensive call (one
Imagen still) for the dispatcher's pre-call cost-cap gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from shipcast.cost import IMAGEN_IMAGE_USD
from shipcast.errors import (
    GeminiSafetyBlocked,
    GeminiTransientError,
    StageInputMissing,
    StageOutputInvalid,
)
from shipcast.manifest import StageStatus
from shipcast.schemas import BrandProposal, ChangelogEntry, MarketingBrief
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from collections.abc import Callable

    from shipcast.clients.gemini_client import AspectRatio
    from shipcast.project import Project

#: Imagen still retry budget (transient 429/5xx only).
_IMAGE_RETRIES: int = 3

#: Upstream artifact relative paths.
_BRIEF_REL: str = "04_plan/brief.json"
_PROPOSAL_REL: str = "03_brand/proposal.json"
_ENTRY_REL: str = "01_pick/entry.json"

#: The real first-screen website screenshot produced by ``s03_brand``. Used as
#: the Gemini ``reference_image_bytes`` so every Imagen card background is
#: grounded in the brand's actual UI/style (not text-to-image from the palette
#: alone). A declared ``s03_brand`` output, so it should always exist; the
#: ``run()`` read is ``None``-guarded defensively.
_STYLE_SHEET_REL: str = "03_brand/style_sheet.png"

#: Fallback palette used only if ``proposal.json`` carries < 3 hex codes
#: (a degenerate brand pack). Matches the s08 caption fallback.
_FALLBACK_PALETTE: tuple[str, str, str] = ("#111111", "#888888", "#EEEEEE")

#: The four aspect-ratio cards: (ratio token, output filename, (width, height)).
#: The dims are the canonical card sizes (TC-12.2). Gemini may return a slightly
#: different size (e.g. 1024x1024 for ``1:1``), so each card is normalised to its
#: canonical dimensions before the overlay is drawn.
_ASPECT_CARDS: tuple[tuple[str, str, tuple[int, int]], ...] = (
    ("1:1", "1x1.png", (1080, 1080)),
    ("16:9", "16x9.png", (1920, 1080)),
    ("9:16", "9x16.png", (1080, 1920)),
    ("4:5", "4x5.png", (1080, 1350)),
)

#: The OG / social card: a single 1200x630 graphic (the ``og`` Imagen aspect).
_OG_CARD: tuple[str, str, tuple[int, int]] = ("og", "og_card.png", (1200, 630))

#: The conditional stat card, rendered at the SAME four aspect ratios as the
#: aspect cards but with the ``stat_`` filename prefix (TC-12.4). Only produced
#: when ``brief.has_stat_card`` is true.
_STAT_CARDS: tuple[tuple[str, str, tuple[int, int]], ...] = (
    ("1:1", "stat_1x1.png", (1080, 1080)),
    ("16:9", "stat_16x9.png", (1920, 1080)),
    ("9:16", "stat_9x16.png", (1080, 1920)),
    ("4:5", "stat_4x5.png", (1080, 1350)),
)


# --------------------------------------------------------------------------- #
# Clients bundle Protocol (structural; mocked in tests)
# --------------------------------------------------------------------------- #


@runtime_checkable
class _GeminiLike(Protocol):
    def generate_image(
        self,
        prompt: str,
        *,
        model: str,
        seed: int,
        reference_image_bytes: bytes | None = ...,
        aspect_ratio: AspectRatio = ...,
    ) -> bytes: ...


class _ClientsBundle(Protocol):
    @property
    def gemini(self) -> _GeminiLike: ...


def _default_clients_factory(project: Project) -> _ClientsBundle:
    """Construct the real Gemini client lazily inside ``run()``."""
    from shipcast.clients.gemini_client import GeminiClient

    gemini = GeminiClient(api_key=project.settings.gemini_api_key)

    class _Bundle:
        def __init__(self) -> None:
            self.gemini: _GeminiLike = gemini

    return _Bundle()


class GraphicsStage(BaseStage):
    """Produce the static graphics package under ``09_graphics/``."""

    id: ClassVar[str] = "09_graphics"
    requires: ClassVar[tuple[str, ...]] = ("04_plan", "03_brand")
    output_schema: ClassVar[type[MarketingBrief]] = MarketingBrief
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Open each aspect card - confirm the headline reads clearly and the "
        "background is on-brand (palette, no off-brand colours).",
        "Confirm every card respects the 8-point grid and >= 8 % padding (the "
        "headline never touches the frame edge).",
        "Verify the OG card and (if present) the stat card / code screenshot / "
        "LinkedIn carousel are legible and on-brand.",
    )

    def __init__(
        self,
        *,
        clients_factory: Callable[[Project], _ClientsBundle] | None = None,
    ) -> None:
        self._clients_factory: Callable[[Project], _ClientsBundle] = (
            clients_factory or _default_clients_factory
        )

    # ------------------------------------------------------------- cost gate
    def next_call_cost_usd(self, project: Project) -> float:
        """Cost of the single most-expensive paid call (one Imagen still)."""
        return IMAGEN_IMAGE_USD

    # ------------------------------------------------------------- inputs
    def _load_brief(self, project: Project) -> MarketingBrief:
        path = project.path / _BRIEF_REL
        if not path.is_file():
            raise StageInputMissing(
                f"stage {self.id!r} requires {_BRIEF_REL} to exist"
            )
        return MarketingBrief.model_validate_json(path.read_text(encoding="utf-8"))

    def _headline(self, project: Project) -> str:
        """The card headline - the entry name (``01_pick/entry.json``).

        ``s09`` requires ``04_plan``, which transitively requires ``01_pick``, so
        ``entry.json`` is guaranteed present on disk. The entry name is the
        cleanest "what shipped" headline; if it is somehow unreadable we fall
        back to the first carousel beat's headline (always present).
        """
        path = project.path / _ENTRY_REL
        if path.is_file():
            entry = ChangelogEntry.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if entry.name.strip():
                return entry.name.strip()
        brief = self._load_brief(project)
        return brief.carousel_beats[0].headline.strip()

    def _palette(self, project: Project) -> tuple[str, str, str]:
        """``(primary, accent, neutral)`` from ``03_brand/proposal.json``."""
        path = project.path / _PROPOSAL_REL
        primary, accent, neutral = _FALLBACK_PALETTE
        if path.is_file():
            proposal = BrandProposal.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            hexes = proposal.palette
            if len(hexes) >= 1:
                primary = hexes[0]
            if len(hexes) >= 2:
                accent = hexes[1]
            if len(hexes) >= 3:
                neutral = hexes[2]
        return primary, accent, neutral

    def _brand_font_path(self, project: Project) -> Path | None:
        """First ``.ttf`` under ``_brand/<slug>/fonts/`` (display font), if any."""
        brand_slug = self._read_brand_slug(project)
        if brand_slug is None:
            return None
        fonts_dir = project.root / "_brand" / brand_slug / "fonts"
        if not fonts_dir.is_dir():
            return None
        fonts = sorted(fonts_dir.glob("*.ttf"))
        return fonts[0] if fonts else None

    @staticmethod
    def _read_brand_slug(project: Project) -> str | None:
        """Read ``brand_slug`` from ``input.yaml`` (None if missing/malformed)."""
        import yaml

        path = project.input_path
        if not path.is_file():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return None
        if isinstance(data, dict):
            value = data.get("brand_slug")
            if isinstance(value, str) and value:
                return value
        return None

    # ------------------------------------------------------------- run
    def run(self, project: Project) -> StageResult:
        """Render the full graphics package.

        Renders the 4 aspect cards (Slice 16) and the always-on OG card plus the
        conditional stat card (Slice 17, gated on ``brief.has_stat_card``).
        Slice 18 extends this shell to call ``_render_code`` /
        ``_render_carousel_slide`` (gated on ``brief.has_code_screenshot``).
        Conditional outputs are declared in the returned ``StageResult`` so the
        dispatcher's outputs-hash + reset cover them.
        """
        clients = self._clients_factory(project)
        brief = self._load_brief(project)
        headline = self._headline(project)
        palette = self._palette(project)
        font_path = self._brand_font_path(project)
        image_model = project.settings.gemini_image_model

        # Read the real brand style-sheet screenshot ONCE and pass it as the
        # Gemini reference image for every Imagen card background, grounding the
        # generated backgrounds in the brand's actual UI. It is a declared
        # s03_brand output so it should always exist; the None guard is defensive.
        style_sheet_path = project.path / _STYLE_SHEET_REL
        ref = (
            style_sheet_path.read_bytes() if style_sheet_path.is_file() else None
        )

        stage_dir = project.stage_dir(self.id)
        stage_dir.mkdir(parents=True, exist_ok=True)

        cost = 0.0
        outputs: list[Path] = []
        #: Imagen-backed cards rendered (the cost-relevant count). The carousel
        #: + code screenshot are pure-PIL and do NOT count here.
        imagen_cards = 0

        for ratio, filename, dims in _ASPECT_CARDS:
            card_path = stage_dir / filename
            self._render_aspect_card(
                clients=clients,
                ratio=ratio,  # type: ignore[arg-type]
                dims=dims,
                headline=headline,
                palette=palette,
                font_path=font_path,
                image_model=image_model,
                reference_image_bytes=ref,
                out_path=card_path,
            )
            cost += IMAGEN_IMAGE_USD
            imagen_cards += 1
            outputs.append(Path(self.id) / filename)

        # OG / social card (Slice 17) — ALWAYS rendered.
        _og_ratio, og_filename, og_dims = _OG_CARD
        self._render_og(
            clients=clients,
            dims=og_dims,
            headline=headline,
            palette=palette,
            font_path=font_path,
            image_model=image_model,
            reference_image_bytes=ref,
            out_path=stage_dir / og_filename,
        )
        cost += IMAGEN_IMAGE_USD
        imagen_cards += 1
        outputs.append(Path(self.id) / og_filename)

        # Conditional stat card (Slice 17) — 4 ratios, ONLY when the brief flag
        # is set. The conditional outputs are declared in the StageResult so the
        # dispatcher's outputs-hash + reset cover them.
        if brief.has_stat_card:
            for ratio, filename, dims in _STAT_CARDS:
                self._render_stat(
                    clients=clients,
                    ratio=ratio,  # type: ignore[arg-type]
                    dims=dims,
                    headline=headline,
                    palette=palette,
                    font_path=font_path,
                    image_model=image_model,
                    reference_image_bytes=ref,
                    out_path=stage_dir / filename,
                )
                cost += IMAGEN_IMAGE_USD
                imagen_cards += 1
                outputs.append(Path(self.id) / filename)

        # LinkedIn carousel (Slice 18) — ALWAYS rendered: exactly 6 slides
        # (slide 01 = the LinkedIn hook, slides 02-05 = the four carousel beats,
        # slide 06 = the first CTA). Pure PIL — no Imagen call.
        carousel_dir = stage_dir / "carousel"
        for idx, (kind, head, body) in enumerate(
            self._carousel_slides(project, brief), start=1
        ):
            slide_rel = f"carousel/slide_{idx:02d}.png"
            self._render_carousel_slide(
                idx=idx,
                kind=kind,
                headline=head,
                body=body,
                palette=palette,
                font_path=font_path,
                out_path=carousel_dir / f"slide_{idx:02d}.png",
            )
            outputs.append(Path(self.id) / slide_rel)

        # Conditional code screenshot (Slice 18) — ONLY when the brief flag is
        # set. Rendered locally (Pygments + PIL) — no external API. The
        # conditional output is declared so the dispatcher's outputs-hash + reset
        # cover it.
        if brief.has_code_screenshot:
            self._render_code(
                project=project,
                palette=palette,
                out_path=stage_dir / "code.png",
            )
            outputs.append(Path(self.id) / "code.png")

        return StageResult(
            status=StageStatus.DONE,
            outputs=tuple(outputs),
            metrics={"cost_usd": round(cost, 4), "cards": imagen_cards},
        )

    # ------------------------------------------------------------- aspect card
    def _render_aspect_card(
        self,
        *,
        clients: _ClientsBundle,
        ratio: AspectRatio,
        dims: tuple[int, int],
        headline: str,
        palette: tuple[str, str, str],
        font_path: Path | None,
        image_model: str,
        reference_image_bytes: bytes | None,
        out_path: Path,
    ) -> None:
        """Render one aspect-ratio card -> ``out_path`` (exact ``dims``).

        Gemini Imagen generates the background at ``ratio`` (conditioned on the
        brand ``reference_image_bytes`` style-sheet screenshot when present); the
        bytes are normalised to the canonical ``dims`` (RGB), then the entry
        ``headline`` is overlaid with the brand display font via ``draw_outlined``
        on the 8-point grid (>= 8 % padding). Delegates to the shared
        :meth:`_render_imagen_card` body.
        """
        self._render_imagen_card(
            clients=clients,
            ratio=ratio,
            dims=dims,
            headline=headline,
            palette=palette,
            font_path=font_path,
            image_model=image_model,
            reference_image_bytes=reference_image_bytes,
            out_path=out_path,
        )

    @staticmethod
    def _overlay_headline(
        background: Any,
        *,
        headline: str,
        palette: tuple[str, str, str],
        font_path: Path | None,
    ) -> None:
        """Draw the wrapped, centred, outlined ``headline`` onto ``background``.

        Shared by ``_render_aspect_card`` / ``_render_og`` / ``_render_stat`` so
        all three cards lay text out identically: brand display font, headline
        sized to the frame's shorter side, snapped to the 8-point grid, wrapped
        inside the >= 8 % padded safe area, and vertically centred. The text is
        ``neutral`` with a ``primary`` stroke so it reads on any background.
        """
        from PIL import ImageDraw

        from shipcast.composition import layout
        from shipcast.composition.captions import _load_font

        width, height = background.size
        draw = ImageDraw.ImageDraw(background)

        pad = layout.min_padding(width, height)
        # Headline size scales with the frame's shorter side; clamped so it never
        # overruns the safe area. Snapped to the 8-point grid for consistency.
        font_size = layout.snap_to_grid(min(width, height) * 0.10)
        font = _load_font(font_size, font_path)

        primary, _accent, neutral = palette
        # Wrap the headline so it fits inside the padded safe width.
        lines = GraphicsStage._wrap_headline(
            headline, font, draw, max_width=width - 2 * pad
        )
        line_height = layout.snap_to_grid(font_size * 1.25)
        block_height = line_height * len(lines)
        # Vertically center the headline block within the safe area.
        top = layout.snap_to_grid((height - block_height) / 2)

        for i, line in enumerate(lines):
            y = top + i * line_height
            layout.draw_outlined(
                draw,
                line,
                (width / 2, y),
                font,
                fill=neutral,
                stroke_fill=primary,
                anchor="mt",
            )

    @staticmethod
    def _background_prompt(
        headline: str, palette: tuple[str, str, str], ratio: AspectRatio
    ) -> str:
        """Build the Imagen background prompt for an aspect card.

        The background must read on-brand (the palette-conformance gate measures
        dE-2000 against the brand hexes), so the prompt names the three brand
        colours explicitly and asks for a clean, text-friendly composition with
        ample negative space for the overlaid headline.
        """
        primary, accent, neutral = palette
        return (
            f"Clean, modern, on-brand marketing background for a software launch "
            f"card ({ratio}), consistent with the supplied brand reference image. "
            f"Use ONLY these brand colours: primary {primary}, accent {accent}, "
            f"neutral {neutral}. Large areas of flat brand colour with ample "
            f"negative space and high contrast for an overlaid headline. No text, "
            f"no logos, no busy detail. Theme: {headline!r}."
        )

    @staticmethod
    def _wrap_headline(
        headline: str,
        font: Any,
        draw: Any,
        *,
        max_width: int,
    ) -> list[str]:
        """Greedy word-wrap so each rendered line fits within ``max_width`` px."""

        def line_width(text: str) -> float:
            bbox = draw.textbbox((0, 0), text, font=font)
            return float(bbox[2] - bbox[0])

        words = headline.split()
        if not words:
            return [headline]
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if line_width(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _generate_still_with_retry(
        self,
        clients: _ClientsBundle,
        prompt: str,
        ratio: AspectRatio,
        image_model: str,
        reference_image_bytes: bytes | None = None,
    ) -> bytes:
        """Call Imagen with a bounded retry on transient errors.

        A :class:`GeminiSafetyBlocked` is re-raised UNWRAPPED so the dispatcher
        records ``error.type == "GeminiSafetyBlocked"``. Transient errors are
        retried; a :class:`GeminiRateLimited` (a transient subtype) that persists
        - or surfaces on a later card - propagates so the dispatcher records the
        FAILED transition (TC-12.10).
        """
        last_exc: BaseException | None = None
        for _attempt in range(_IMAGE_RETRIES):
            try:
                return clients.gemini.generate_image(
                    prompt,
                    model=image_model,
                    seed=0,
                    reference_image_bytes=reference_image_bytes,
                    aspect_ratio=ratio,
                )
            except GeminiSafetyBlocked:
                raise
            except GeminiTransientError as exc:
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------- Slice 17-18 stubs
    # The following five renderers complete the graphics package. Their bodies
    # land in Slices 17 (OG + stat) and 18 (code + carousel); until then they
    # are NotImplementedError-guarded and are NOT called by ``run()``.

    def _render_og(
        self,
        *,
        clients: _ClientsBundle,
        dims: tuple[int, int],
        headline: str,
        palette: tuple[str, str, str],
        font_path: Path | None,
        image_model: str,
        reference_image_bytes: bytes | None,
        out_path: Path,
    ) -> None:
        """Render the OG / social card (1200x630) -> ``out_path``.

        Gemini Imagen generates the background at the ``og`` aspect (1200x630),
        conditioned on the brand ``reference_image_bytes`` style-sheet screenshot
        when present; the bytes are normalised to ``dims`` (RGB), then the entry
        ``headline`` is overlaid with the brand display font via the shared
        :meth:`_overlay_headline` (same 8-point-grid / >= 8 % padding rules as
        the aspect cards). This is the card that fronts a shared link.
        """
        self._render_imagen_card(
            clients=clients,
            ratio="og",
            dims=dims,
            headline=headline,
            palette=palette,
            font_path=font_path,
            image_model=image_model,
            reference_image_bytes=reference_image_bytes,
            out_path=out_path,
        )

    def _render_stat(
        self,
        *,
        clients: _ClientsBundle,
        ratio: AspectRatio,
        dims: tuple[int, int],
        headline: str,
        palette: tuple[str, str, str],
        font_path: Path | None,
        image_model: str,
        reference_image_bytes: bytes | None,
        out_path: Path,
    ) -> None:
        """Render one conditional stat card at ``ratio`` -> ``out_path``.

        Called once per aspect ratio ONLY when ``brief.has_stat_card`` is true
        (the dispatcher gates the cost; ``run()`` gates the call). The render
        path mirrors the aspect card: an on-brand Imagen background (conditioned
        on the brand ``reference_image_bytes`` style-sheet screenshot when
        present) normalised to ``dims`` with the headline overlaid via
        :meth:`_overlay_headline`.
        """
        self._render_imagen_card(
            clients=clients,
            ratio=ratio,
            dims=dims,
            headline=headline,
            palette=palette,
            font_path=font_path,
            image_model=image_model,
            reference_image_bytes=reference_image_bytes,
            out_path=out_path,
        )

    def _render_imagen_card(
        self,
        *,
        clients: _ClientsBundle,
        ratio: AspectRatio,
        dims: tuple[int, int],
        headline: str,
        palette: tuple[str, str, str],
        font_path: Path | None,
        image_model: str,
        reference_image_bytes: bytes | None,
        out_path: Path,
    ) -> None:
        """Imagen background (at ``ratio``) + headline overlay -> ``out_path``.

        The shared body behind :meth:`_render_aspect_card` / :meth:`_render_og` /
        :meth:`_render_stat`: generate the still with the bounded transient retry
        (conditioned on the brand ``reference_image_bytes`` style-sheet screenshot
        when present), normalise to the canonical ``dims`` (RGB), overlay the
        headline, and save as PNG.
        """
        from io import BytesIO

        from PIL import Image

        prompt = self._background_prompt(headline, palette, ratio)
        raw = self._generate_still_with_retry(
            clients, prompt, ratio, image_model, reference_image_bytes
        )

        with Image.open(BytesIO(raw)) as src:
            background = src.convert("RGB")
            if background.size != dims:
                background = background.resize(dims)
            else:
                background = background.copy()

        self._overlay_headline(
            background, headline=headline, palette=palette, font_path=font_path
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        background.save(out_path, format="PNG")

    # ------------------------------------------------------- Slice 18 carousel
    def _carousel_slides(
        self, project: Project, brief: MarketingBrief
    ) -> list[tuple[str, str, str]]:
        """Return the six ``(kind, headline, body)`` carousel slide specs.

        Fixed mapping (CLAUDE.md): slide 01 = the LinkedIn hook (rendered from
        the brief's ``hook_template_per_channel["linkedin"]`` over the picked
        entry), slides 02-05 = the four ``carousel_beats``, slide 06 = the first
        CTA. The 4-beat count is schema-enforced, so the result is always 6.
        """
        from shipcast.marketing import hooks

        entry = self._entry_mapping(project)
        hook_key = brief.hook_template_per_channel["linkedin"]
        hook_text = hooks.render(hook_key, entry)
        cta = next((c for c in brief.ctas if c.strip()), brief.ctas[0])

        slides: list[tuple[str, str, str]] = [("hook", hook_text, "")]
        for beat in brief.carousel_beats:
            slides.append(("beat", beat.headline, beat.body))
        slides.append(("cta", cta, ""))
        return slides

    def _entry_mapping(self, project: Project) -> dict[str, str]:
        """``{name, summary, details}`` from ``01_pick/entry.json`` (best-effort)."""
        path = project.path / _ENTRY_REL
        if path.is_file():
            entry = ChangelogEntry.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            return {
                "name": entry.name,
                "summary": entry.summary,
                "details": entry.details,
            }
        return {"name": "", "summary": "", "details": ""}

    def _render_carousel_slide(
        self,
        *,
        idx: int,
        kind: str,
        headline: str,
        body: str,
        palette: tuple[str, str, str],
        font_path: Path | None,
        out_path: Path,
    ) -> None:
        """Render one LinkedIn carousel slide (1080x1350) -> ``out_path``.

        Delegates to the pure :func:`shipcast.marketing.carousel.render_slide`
        composer (no external API). Indirected through the module attribute so a
        test can spy on the composer to assert slide 01 got the hook and slide 06
        got the CTA (TC-12.8).
        """
        from shipcast.marketing import carousel

        carousel.render_slide(
            idx,
            kind=kind,  # type: ignore[arg-type]
            headline=headline,
            body=body,
            palette=palette,
            font_path=font_path,
            out_path=out_path,
        )

    # ------------------------------------------------------- Slice 18 code shot
    def _render_code(
        self,
        *,
        project: Project,
        palette: tuple[str, str, str],
        out_path: Path,
    ) -> None:
        """Render the conditional code screenshot -> ``out_path`` (Pygments+PIL).

        The snippet is the FIRST fenced code block in the picked entry's
        ``details``; when the entry carries no fenced block a small,
        representative snippet is synthesized from the entry name so the card
        still renders. Delegates to the pure
        :func:`shipcast.marketing.code_screenshot.render_code` — no external API.
        """
        from shipcast.marketing import code_screenshot

        entry = self._entry_mapping(project)
        extracted = code_screenshot.extract_code_block(entry.get("details", ""))
        if extracted is not None:
            code, language = extracted
        else:
            code, language = self._synthesize_snippet(entry), "python"

        code_screenshot.render_code(
            code,
            language=language,
            palette=palette,
            out_path=out_path,
        )

    @staticmethod
    def _synthesize_snippet(entry: dict[str, str]) -> str:
        """A tiny, deterministic placeholder snippet when no fenced block exists.

        Built only from the picked entry's name so the same entry always yields
        the same snippet (determinism); never includes secrets or live data.
        """
        name = (entry.get("name") or "feature").strip() or "feature"
        slug = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip(
            "_"
        ) or "feature"
        return (
            f"def {slug}() -> str:\n"
            f'    """Ship: {name}."""\n'
            f'    return "{name}"\n'
        )

    # ------------------------------------------------------------- outputs
    def validate_outputs(self, project: Project, result: StageResult) -> None:
        """Path-safety on every PNG output.

        The outputs are PNGs (not a single JSON), so the default single-output
        schema check does not apply; we run the shared path-traversal guard over
        all outputs and confirm each is a non-empty PNG.
        """
        self._validate_output_paths(project, result)
        for rel in result.outputs:
            full = (project.path / rel).resolve()
            if full.suffix != ".png" or full.stat().st_size == 0:
                raise StageOutputInvalid(
                    f"stage {self.id!r} output {rel!r} is not a non-empty PNG"
                )
