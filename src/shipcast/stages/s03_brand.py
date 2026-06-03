"""Stage 03 — brand.

Turns the operator's brand pack (``projects/_brand/<brand_slug>/``) plus the
live app into the brand contract every downstream creative stage reads:

* ``03_brand/proposal.json`` — :class:`BrandProposal` (palette / font / logo flag).
* ``03_brand/logo.png``      — the live-app logo, OR a 1x1 transparent placeholder.
* ``03_brand/style_sheet.png`` — a Gemini-generated 1:1 style sheet (or the
  operator-supplied one copied verbatim when the pack already has it).
* ``03_brand/voice.md``      — a COPY of ``_brand/<slug>/voice.md`` (Finding 1).

``run`` flow (UC-4):

1. Validate the brand pack — :class:`BrandPackIncomplete` (listing every missing
   REQUIRED file) BEFORE any external API call. ``check_inputs`` performs this so
   an incomplete pack never reaches ``run`` / costs money (TC-6.2..6.5).
2. Palette + font:
   * IF ``palette.hint.json`` is present → use its three values directly and SKIP
     the Playwright extract ENTIRELY (no navigation, no SSRF surface — UC-4-A1).
   * ELSE → validate ``live_url`` (the Playwright client re-validates before any
     ``goto``) and extract the top-≤5 hex palette + body font.
3. Logo — ``screenshot_logo``; on ``None`` write a 1x1 transparent PNG and set
   ``logo_detected=false`` (UC-4-A3).
4. Style sheet — Gemini ``generate_image(aspect_ratio="1:1")``; SKIPPED (copied)
   when the pack ships its own ``style_sheet.png`` (UC-4-A2).
5. Write ``proposal.json`` (validated against :class:`BrandProposal`) and copy
   ``voice.md``.

Architect MAJOR Finding 1 — voice.md read-path (remediation option (a))
-----------------------------------------------------------------------
``s03_brand`` copies ``_brand/<slug>/voice.md`` to ``03_brand/voice.md`` as a
FOURTH DECLARED OUTPUT. Downstream stages (``s04_plan`` / ``s08_video`` /
``s10_copy``) read the CANONICAL ``03_brand/voice.md`` — never the raw pack —
so ``compute_outputs_hash`` covers operator edits and ``check_inputs`` (which
verifies declared upstream outputs exist on disk) catches a deleted ``voice.md``
(TC-6.16 / TC-20.1 / TC-20.2).

Brand data never enters ``config_snapshot``
-------------------------------------------
This stage writes brand bytes ONLY as files under ``03_brand/``. It never calls
``update_config_snapshot`` and the dispatcher never folds outputs into the
snapshot, so ``manifest.config_snapshot`` is byte-identical before and after the
run (TC-6.13). Brand drift is covered by ``inputs_hash`` via
``additional_input_paths`` (the brand-pack files), NOT by the snapshot.

Lazy clients
------------
Playwright + Gemini are obtained from the injected ``clients_factory`` inside
``run()`` only (never at import). Tests inject mocks; the default factory builds
the real clients lazily so importing this module pulls neither ``requests`` nor
``playwright`` into ``sys.modules`` (import-purity invariant).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, runtime_checkable

from shipcast.brand import extractor, loader
from shipcast.cost import IMAGEN_IMAGE_USD
from shipcast.manifest import StageStatus, dump_json_canonical
from shipcast.schemas import BrandProposal, InputYaml
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from shipcast.brand.loader import BrandPack
    from shipcast.project import Project


# --------------------------------------------------------------------------- #
# Clients bundle Protocol (structural; mocked in tests)
# --------------------------------------------------------------------------- #

#: Mirror of ``gemini_client.AspectRatio`` (kept local so importing this stage
#: does not couple to the heavy client module at import time).
AspectRatio = Literal["1:1", "16:9", "9:16", "4:5", "og"]


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


@runtime_checkable
class _PlaywrightLike(Protocol):
    def extract_css_palette(self, url: str) -> list[str]: ...
    def extract_font_family(self, url: str) -> str: ...
    def screenshot_logo(self, url: str) -> bytes | None: ...


class _ClientsBundle(Protocol):
    """Structural type for the bundle returned by ``clients_factory``."""

    @property
    def gemini(self) -> _GeminiLike: ...

    @property
    def playwright(self) -> _PlaywrightLike: ...


def _default_clients_factory(project: Project) -> _ClientsBundle:
    """Construct the real client bundle lazily inside ``run()``.

    Heavy imports (``GeminiClient`` → ``requests``; ``PlaywrightClient`` →
    ``playwright``) live INSIDE their methods, so importing this module keeps
    both out of ``sys.modules`` (import-purity test).
    """
    from shipcast.clients.gemini_client import GeminiClient
    from shipcast.clients.playwright_client import PlaywrightClient

    gemini = GeminiClient(api_key=project.settings.gemini_api_key)
    playwright = PlaywrightClient()

    class _Bundle:
        def __init__(self) -> None:
            self.gemini: _GeminiLike = gemini
            self.playwright: _PlaywrightLike = playwright

    return _Bundle()


class BrandStage(BaseStage):
    """Extract the brand contract into ``03_brand/`` (Finding-1 voice.md copy)."""

    id: ClassVar[str] = "03_brand"
    requires: ClassVar[tuple[str, ...]] = ("02_enrich",)
    output_schema: ClassVar[type[BrandProposal]] = BrandProposal
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Edit proposal.json down to EXACTLY 3 hex colors (primary, accent, neutral).",
        "If logo_detected is false, replace logo.png with the real brand logo before approving.",
        "Confirm style_sheet.png matches the brand; supply your own if undesirable.",
        "Confirm voice.md reflects the tone, banned phrases, and caption_mode you want.",
    )

    PROPOSAL_FILENAME: ClassVar[str] = "proposal.json"
    LOGO_FILENAME: ClassVar[str] = "logo.png"
    STYLE_SHEET_FILENAME: ClassVar[str] = "style_sheet.png"
    VOICE_FILENAME: ClassVar[str] = "voice.md"

    #: The image model used for the 1:1 style sheet. Stage-local constant so the
    #: stage does not depend on Settings field names beyond the api key.
    STYLE_SHEET_MODEL: ClassVar[str] = "gemini-2.5-flash-image-preview"

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
        """The one paid call this stage may make: the Gemini style-sheet image.

        Declared unconditionally as a TRUE pre-condition (the dispatcher gates
        BEFORE ``run``, so it cannot know yet whether the pack ships its own
        ``style_sheet.png``). $0.04 is the most this stage can spend.
        """
        return IMAGEN_IMAGE_USD

    # ------------------------------------------------------------- inputs
    def check_inputs(self, project: Project) -> None:
        """Upstream gate + brand-pack completeness (BEFORE any API call).

        Runs the default upstream check (``02_enrich`` done+approved, its outputs
        present) THEN validates the brand pack — raising
        :class:`~shipcast.errors.BrandPackIncomplete` (listing every missing
        REQUIRED file) before ``run`` is ever reached. This is the gate that
        guarantees an incomplete pack never constructs or calls a client
        (TC-6.2..6.5).
        """
        super().check_inputs(project)
        # Read brand_slug WITHOUT triggering full InputYaml validation: the
        # live_url SSRF check resolves DNS, which (a) must not run at gate time
        # and (b) would mask BrandPackIncomplete behind a network error on the
        # incomplete-pack tests (TC-6.2..6.5, which set up no DNS). live_url is
        # validated in run() — and the Playwright client re-validates before any
        # goto() (TC-6.10).
        brand_slug = self._read_brand_slug(project)
        # Raises BrandPackIncomplete / ValueError(malformed hint) on a bad pack.
        loader.validate(project.root, brand_slug)

    def additional_input_paths(self, project: Project) -> Iterable[Path]:
        """Every brand-pack file, so ``inputs_hash`` covers brand drift.

        The pack lives OUTSIDE the upstream stage's outputs (it is operator-
        placed under ``_brand/<slug>/``), so it is surfaced here per the
        ``BaseStage.additional_input_paths`` hook contract. An edit to any pack
        file (voice.md, a font, the logo, palette.hint.json, style_sheet.png)
        invalidates the recorded ``inputs_hash``.
        """
        try:
            brand_slug = self._read_brand_slug(project)
            pack = loader.validate(project.root, brand_slug)
        except Exception:
            # During cost-cap / pre-run hashing the pack may be incomplete; the
            # real BrandPackIncomplete is surfaced by check_inputs. Return no
            # extra paths rather than crash the (audit-only) hash computation.
            return ()
        return pack.input_paths()

    def _read_raw_input(self, project: Project) -> dict[str, object]:
        """Read ``input.yaml`` into a cleaned mapping WITHOUT schema validation.

        Used at gate time (``check_inputs``) so reading ``brand_slug`` does not
        trigger the ``live_url`` SSRF validator's DNS lookup. Full validation
        (including the URL defense) happens in :meth:`_load_input` during
        ``run()``.
        """
        import yaml

        from shipcast.errors import StageInputMissing

        if not project.input_path.is_file():
            raise StageInputMissing(
                f"stage {self.id!r} requires {project.input_path} to exist"
            )
        raw = project.input_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError(
                f"{project.input_path} must contain a YAML mapping, "
                f"got {type(data).__name__}"
            )
        return {k: v for k, v in data.items() if v is not None}

    def _read_brand_slug(self, project: Project) -> str:
        """Extract ``brand_slug`` from ``input.yaml`` without full validation."""
        value = self._read_raw_input(project).get("brand_slug")
        if not isinstance(value, str) or not value:
            raise ValueError("input.yaml is missing the required 'brand_slug' field")
        return value

    def _load_input(self, project: Project) -> InputYaml:
        """Read + FULLY validate ``input.yaml`` (SSRF + path defenses; run-time)."""
        return InputYaml.model_validate(self._read_raw_input(project))

    # ------------------------------------------------------------- run
    def run(self, project: Project) -> StageResult:
        """Validate the pack, extract brand, write the four ``03_brand/`` outputs.

        Determinism note: the JSON wrapper carries no ``datetime.now`` / random
        id; non-determinism is confined to the (LLM image) ``run`` call body.
        """
        spec = self._load_input(project)
        pack = loader.validate(project.root, spec.brand_slug)
        clients = self._clients_factory(project)

        stage_dir = project.stage_dir(self.id)
        stage_dir.mkdir(parents=True, exist_ok=True)

        # (a) palette + font — palette.hint.json SKIPS Playwright entirely.
        palette, font_family = self._resolve_palette_and_font(spec, pack, clients)

        # (b) logo — None → 1x1 transparent placeholder + logo_detected=false.
        logo_detected = self._resolve_logo(project, spec, pack, clients)

        # (c) style sheet — operator-supplied one SKIPS the paid Gemini call.
        style_cost = self._resolve_style_sheet(project, pack, clients, palette)

        # (d) voice.md — Finding 1: copy as a declared, hash-covered output.
        voice_dest = project.artifact_path(self.id, self.VOICE_FILENAME)
        shutil.copyfile(pack.voice_md, voice_dest)

        # (e) proposal.json
        proposal = BrandProposal(
            palette=palette,
            font_family=font_family,
            logo_detected=logo_detected,
        )
        proposal_path = project.artifact_path(self.id, self.PROPOSAL_FILENAME)
        proposal_path.write_text(
            dump_json_canonical(proposal.model_dump(mode="json")),
            encoding="utf-8",
        )

        outputs: tuple[Path, ...] = (
            Path(self.id) / self.PROPOSAL_FILENAME,
            Path(self.id) / self.LOGO_FILENAME,
            Path(self.id) / self.STYLE_SHEET_FILENAME,
            Path(self.id) / self.VOICE_FILENAME,
        )
        return StageResult(
            status=StageStatus.DONE,
            outputs=outputs,
            metrics={
                "cost_usd": style_cost,
                "logo_detected": logo_detected,
                "palette_size": len(palette),
                "palette_from_hint": pack.palette_hint is not None,
            },
        )

    # ------------------------------------------------------------- sub-steps
    def _resolve_palette_and_font(
        self,
        spec: InputYaml,
        pack: BrandPack,
        clients: _ClientsBundle,
    ) -> tuple[list[str], str]:
        """Return ``(palette, font_family)``.

        ``palette.hint.json`` present → use its three values and SKIP Playwright
        (no navigation). Otherwise validate ``live_url`` is present and extract
        via Playwright (the client re-validates the URL before any ``goto``).
        """
        if pack.palette_hint is not None:
            hint = pack.palette_hint
            return [hint["primary"], hint["accent"], hint["neutral"]], "Inter"

        if spec.live_url is None:
            raise ValueError(
                "s03_brand requires either a palette.hint.json in the brand pack "
                "or a live_url in input.yaml to extract the palette from"
            )
        result = extractor.extract_palette_and_font(
            clients.playwright, str(spec.live_url)
        )
        return result.palette, result.font_family

    def _resolve_logo(
        self,
        project: Project,
        spec: InputYaml,
        pack: BrandPack,
        clients: _ClientsBundle,
    ) -> bool:
        """Write ``03_brand/logo.png`` and return whether a real logo was found.

        When the palette came from the hint AND there is no ``live_url``, there
        is no live app to screenshot — write the pack's own logo (rasterized via
        the placeholder path is wrong for SVG, so we copy a PNG logo, else fall
        back to the transparent placeholder and flag not-detected).
        """
        logo_dest = project.artifact_path(self.id, self.LOGO_FILENAME)

        if spec.live_url is not None:
            result = extractor.logo_png_bytes(clients.playwright, str(spec.live_url))
            extractor.write_png(logo_dest, result.png_bytes)
            return result.detected

        # No live URL (hint-only path): prefer a PNG logo from the pack; else
        # write the transparent placeholder so the operator supplies one.
        if pack.logo.suffix.lower() == ".png":
            extractor.write_png(logo_dest, pack.logo.read_bytes())
            return True
        extractor.write_png(logo_dest, extractor.transparent_1x1_png())
        return False

    def _resolve_style_sheet(
        self,
        project: Project,
        pack: BrandPack,
        clients: _ClientsBundle,
        palette: list[str],
    ) -> float:
        """Write ``03_brand/style_sheet.png``; return the cost incurred (USD).

        Operator-supplied ``style_sheet.png`` in the pack → copy verbatim, SKIP
        the paid Gemini call (returns 0.0). Otherwise generate a 1:1 style sheet
        via Gemini (returns the Imagen unit cost).
        """
        dest = project.artifact_path(self.id, self.STYLE_SHEET_FILENAME)
        if pack.style_sheet is not None:
            shutil.copyfile(pack.style_sheet, dest)
            return 0.0
        prompt = self._style_sheet_prompt(palette)
        image_bytes = clients.gemini.generate_image(
            prompt,
            model=self.STYLE_SHEET_MODEL,
            seed=0,
            aspect_ratio="1:1",
        )
        extractor.write_png(dest, image_bytes)
        return IMAGEN_IMAGE_USD

    @staticmethod
    def _style_sheet_prompt(palette: list[str]) -> str:
        """Deterministic 1:1 style-sheet prompt from the resolved palette."""
        colors = ", ".join(palette) if palette else "a balanced brand palette"
        return (
            "A clean 1:1 brand style sheet showing color swatches, typography "
            "samples, and component spacing on an 8-point grid. Use this palette: "
            f"{colors}. Minimal, modern, high-contrast, generous padding."
        )

    # ------------------------------------------------------------- validate
    def validate_outputs(self, project: Project, result: StageResult) -> None:
        """Path-safety on all four outputs + schema-check ``proposal.json`` only.

        The default ``validate_outputs`` only schema-checks when there is exactly
        ONE output; this stage emits four (one JSON + two PNGs + one markdown),
        so we run the shared path-traversal guard over every declared output and
        validate ``proposal.json`` against :class:`BrandProposal` explicitly.
        """
        import json

        from pydantic import ValidationError

        from shipcast.errors import StageOutputInvalid

        self._validate_output_paths(project, result)
        proposal_rel = Path(self.id) / self.PROPOSAL_FILENAME
        full = (project.path / proposal_rel).resolve()
        try:
            data = json.loads(full.read_text(encoding="utf-8"))
            BrandProposal.model_validate(data)
        except json.JSONDecodeError as exc:
            raise StageOutputInvalid(
                f"stage {self.id!r} output {full} is not valid JSON: {exc}"
            ) from exc
        except ValidationError as exc:
            raise StageOutputInvalid(
                f"stage {self.id!r} output {full} failed schema validation: {exc}"
            ) from exc
