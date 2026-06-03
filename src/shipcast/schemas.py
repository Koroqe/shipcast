"""Pydantic v2 models for shipcast stage artifacts.

LEAF MODULE — this file imports ONLY stdlib + pydantic. It MUST NOT import
from ``shipcast.cost``, ``shipcast.config``, ``shipcast.clients``,
``shipcast.cli``, or ``shipcast.manifest`` (architect Module-Boundary Risk 1).
Other ``shipcast.*`` modules import schemas, never the reverse.

Slice 3 lands ``InputYaml`` + ``WalkthroughStep`` (input.yaml validation,
SSRF + path-traversal defenses) and ``WordTimestamp`` (so the lazy import in
``clients/whisperx_client.py`` resolves for real). Later slices extend this
module with the remaining artifact models.
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
# Module-level constants (monkeypatchable in tests)
# --------------------------------------------------------------------------- #

#: Allowed root every ``repo_path`` must live under (after symlink resolution).
#: Production default; tests monkeypatch this to a tmp dir for accept cases.
ALLOWED_REPO_ROOT: Path = Path("/Users/aleksei/Documents/Projects.nosync")

#: Walkthrough actions Playwright is permitted to perform.
_ALLOWED_ACTIONS = ("goto", "click", "type", "wait", "screenshot")


# --------------------------------------------------------------------------- #
# Shared live_url validator (SSRF defense — reused by PlaywrightClient)
# --------------------------------------------------------------------------- #


def validate_live_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is a safe, public ``https`` target.

    This is the single source of truth for the ``live_url`` SSRF defense. The
    :class:`InputYaml` ``live_url`` field validator delegates here, and
    ``shipcast.clients.playwright_client.PlaywrightClient`` calls it at the top
    of EVERY navigating method BEFORE touching the browser (Slice-8 security
    pre-review: "URL validator runs before any ``goto()``").

    Checks, in order (first failure raises):

    1. Scheme MUST be ``https`` — checked FIRST so a bad scheme never triggers a
       DNS lookup (no network side effect on scheme rejection).
    2. A hostname MUST be present.
    3. The hostname is resolved with :func:`socket.getaddrinfo` (IPv4 *and*
       IPv6 — not the IPv4-only ``gethostbyname``) and EVERY resolved address is
       rejected if it is private (RFC1918 10/8, 172.16/12, 192.168/16), loopback
       (127/8, ``::1``), link-local (169.254/16, ``fe80::/10``), unique-local
       IPv6 (``fc00::/7`` → ``is_private``), unspecified (``0.0.0.0``/``::``),
       reserved, or multicast. Resolving every address closes the IPv6-only-AAAA
       SSRF gap and lets IPv6-literal hosts (``[::1]``) reject cleanly.

    Raising ``ValueError`` (not a Pydantic-specific type) keeps this a leaf
    helper usable both inside a ``field_validator`` (Pydantic wraps it into a
    ``ValidationError``) and directly by the Playwright client.
    """
    parts = urlsplit(url)

    # 1. Scheme check — MUST run before any DNS lookup.
    if parts.scheme != "https":
        raise ValueError(
            f"live_url must use the 'https' scheme, got {parts.scheme!r}"
        )

    host = parts.hostname
    if not host:
        raise ValueError("live_url must include a hostname")

    # 2. Resolve the hostname to EVERY address (IPv4 + IPv6) and reject if ANY
    #    of them is non-public.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(
            f"live_url hostname {host!r} could not be resolved: {exc}"
        ) from exc
    for info in infos:
        resolved = info[4][0]
        ip = ipaddress.ip_address(resolved)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise ValueError(
                f"live_url resolves to a non-public address ({resolved}) — "
                "refusing (SSRF defense)"
            )


# --------------------------------------------------------------------------- #
# WalkthroughStep
# --------------------------------------------------------------------------- #


class WalkthroughStep(BaseModel):
    """One operator-authored step of a Playwright feature walkthrough.

    ``selector`` carrying a ``javascript:`` scheme is rejected to prevent
    arbitrary script injection through the walkthrough config.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["goto", "click", "type", "wait", "screenshot"]
    selector: str | None = None
    value: str | None = None

    @field_validator("selector")
    @classmethod
    def _reject_javascript_selector(cls, v: str | None) -> str | None:
        if v is not None and "javascript:" in v.lower():
            raise ValueError("selector must not contain a 'javascript:' scheme")
        return v


# --------------------------------------------------------------------------- #
# InputYaml
# --------------------------------------------------------------------------- #


class InputYaml(BaseModel):
    """Validated ``input.yaml`` for one shipcast project.

    Security-critical validators (see ``.claude/rules/security.md``):

    * ``live_url`` — https-only; the *resolved* IPv4 must not be private,
      loopback, or link-local (SSRF defense). The scheme check runs FIRST so a
      bad scheme never triggers a DNS lookup.
    * ``repo_path`` — no ``..`` segments; must resolve (symlinks included) to a
      path under :data:`ALLOWED_REPO_ROOT` and contain a ``CHANGELOG.md``.
    """

    model_config = ConfigDict(extra="forbid")

    repo_path: Path
    entry_heading: str
    live_url: AnyUrl | None = None
    brand_slug: str
    video_mode: Literal["standard", "premium"] = "standard"
    feature_walkthrough: list[WalkthroughStep] | None = None

    @field_validator("entry_heading")
    @classmethod
    def _non_empty_heading(cls, v: str) -> str:
        if len(v) < 1:
            raise ValueError("entry_heading must not be empty")
        return v

    @field_validator("live_url")
    @classmethod
    def _validate_live_url(cls, v: AnyUrl | None) -> AnyUrl | None:
        if v is None:
            return v
        # Delegate to the shared SSRF helper — the SAME function the Playwright
        # client invokes before any navigation, so the defense cannot drift.
        validate_live_url(str(v))
        return v

    @model_validator(mode="after")
    def _validate_repo_path(self) -> InputYaml:
        raw = self.repo_path

        # 1. Reject any '..' segment BEFORE touching the filesystem.
        if ".." in raw.parts:
            raise ValueError("repo_path must not contain '..' segments")

        allowed_root = ALLOWED_REPO_ROOT.resolve()

        # 2. Pre-resolution allowed-root check (literal path).
        if not _is_relative_to(raw, allowed_root):
            raise ValueError(
                f"repo_path must be under {allowed_root} (got {raw})"
            )

        # 3. Resolve symlinks and re-check the allowed-root (symlink-escape
        #    defense — a symlink under the root may point outside it).
        resolved = raw.resolve()
        if not _is_relative_to(resolved, allowed_root):
            raise ValueError(
                "repo_path resolves outside the allowed root "
                f"{allowed_root} (resolved to {resolved})"
            )

        # 4. The repo must contain a CHANGELOG.md.
        if not (resolved / "CHANGELOG.md").is_file():
            raise ValueError(
                f"repo_path must contain a CHANGELOG.md (looked in {resolved})"
            )
        return self


# --------------------------------------------------------------------------- #
# ChangelogEntry (artifact produced by changelog/parser.py + s01_pick)
# --------------------------------------------------------------------------- #


class ChangelogEntry(BaseModel):
    """One parsed entry from a target project's ``CHANGELOG.md``.

    The canonical changelog format (see ``~/.claude/rules/changelog.md``) groups
    entries under ``## YYYY-MM-DD`` day headings, each entry headed by
    ``### <name> — HH:MM UTC`` and followed by ``**Summary:**`` / ``**Details:**``
    lines. ``time_utc`` is ``None`` when the heading omits the ``— HH:MM UTC``
    suffix. ``raw`` preserves the verbatim markdown of the entry (heading through
    the last body line) for downstream stages that want the original text.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    date: str
    time_utc: str | None = None
    summary: str = ""
    details: str = ""
    raw: str = ""


# --------------------------------------------------------------------------- #
# WordTimestamp (consumed lazily by clients/whisperx_client.py)
# --------------------------------------------------------------------------- #


class WordTimestamp(BaseModel):
    """A single word with WhisperX-aligned start/end timestamps."""

    model_config = ConfigDict(extra="forbid")

    word: str
    start_sec: float
    end_sec: float
    confidence: float | None = None


# --------------------------------------------------------------------------- #
# EnrichedContext (s02_enrich artifact)
# --------------------------------------------------------------------------- #


class EnrichedContext(BaseModel):
    """The ``02_enrich/context.json`` artifact written by ``s02_enrich``.

    **Architect MAJOR Finding 3 — single source of truth.** The enrichment
    narrative is stored in EXACTLY ONE place: this model's ``narrative`` field
    (serialized into ``02_enrich/context.json``). ``s02_enrich`` deliberately
    does NOT also write a sibling ``narrative.md``; a second on-disk copy would
    be an undeclared, un-hash-covered duplicate that could silently drift from
    ``context.json`` (TC-5.9 / TC-20.4). ``context.json`` is the only declared
    output, so ``compute_outputs_hash`` covers every byte of the narrative.

    Fields:
    * ``pr_links`` — URLs of merged PRs gathered via ``gh pr list`` (may be []).
    * ``diff_stats`` — aggregate ``git log --stat`` numbers (files/insertions/…)
      plus any per-path detail the stage chooses to record.
    * ``narrative`` — the Gemini-multimodal-generated marketing narrative. The
      SOLE copy of the narrative text (Finding 3). Must be non-empty.
    * ``screenshots`` — project-relative paths to captured ``.png`` files under
      ``02_enrich/screenshots/``. Empty when ``live_url`` was omitted (the
      Playwright sub-step is skipped and logged — UC-3-A1).
    * ``ba_framing`` — high-level framing notes from the ``ba-analyst`` sub-agent
      (free-form, retained for downstream copy/plan stages).
    """

    model_config = ConfigDict(extra="forbid")

    pr_links: list[str] = []
    diff_stats: dict[str, object] = {}
    narrative: str
    screenshots: list[str] = []
    ba_framing: dict[str, object] = {}

    @field_validator("narrative")
    @classmethod
    def _narrative_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("narrative must be a non-empty string (Finding 3 sole copy)")
        return v


# --------------------------------------------------------------------------- #
# BrandProposal (s03_brand artifact)
# --------------------------------------------------------------------------- #


class BrandProposal(BaseModel):
    """The ``03_brand/proposal.json`` artifact written by ``s03_brand``.

    The proposal is the machine-readable summary of the brand extraction. The
    binary brand artifacts (``logo.png``, ``style_sheet.png``, ``voice.md``)
    live as sibling files in ``03_brand/`` and are declared outputs alongside
    this JSON; ``compute_outputs_hash`` covers all of them, so an operator edit
    to any one is detected by ``shipcast approve`` (FR-3.10).

    **Brand data never enters ``config_snapshot``** (architecture invariant): the
    proposal and its sibling files are the ONLY home for brand bytes; downstream
    stages read them off disk, and ``inputs_hash`` (via
    ``additional_input_paths``) covers brand-pack drift.

    Fields:
    * ``palette`` — hex color candidates. Right after the run this is the
      top-≤5 extracted (or the 3 ``palette.hint.json`` values); the operator is
      expected to hand-edit down to exactly 3 (primary/accent/neutral) before
      approving (UC-4 step 5). Each entry must be a ``#rrggbb`` hex string.
    * ``font_family`` — the live app's body ``font-family`` string (or a brand
      default when the operator pre-seeds it).
    * ``logo_detected`` — ``False`` when no logo was found on the live app and a
      1x1 transparent placeholder PNG was written instead (UC-4-A3); the
      operator must supply a real ``logo.png`` before approving.
    """

    model_config = ConfigDict(extra="forbid")

    palette: list[str]
    font_family: str
    logo_detected: bool

    @field_validator("palette")
    @classmethod
    def _palette_hex(cls, v: list[str]) -> list[str]:
        for color in v:
            c = color.strip().lower()
            if not (
                c.startswith("#")
                and len(c) == 7
                and all(ch in "0123456789abcdef" for ch in c[1:])
            ):
                raise ValueError(
                    f"palette entries must be '#rrggbb' hex strings, got {color!r}"
                )
        return v


# --------------------------------------------------------------------------- #
# Hook catalog keys (FROZEN value space for MarketingBrief.hook_template_*)
# --------------------------------------------------------------------------- #

#: The seven hook-template keys. Duplicated here as a literal tuple (rather than
#: imported from ``shipcast.marketing.hooks``) to keep ``schemas`` a LEAF module
#: — it imports ONLY stdlib + pydantic (Module-Boundary Risk 1). The
#: ``test_hooks_catalog`` unit test asserts this tuple matches
#: ``hooks.KEYS`` exactly, so the two cannot drift.
HOOK_TEMPLATE_KEYS: tuple[str, ...] = (
    "we_just_shipped",
    "before_after",
    "problem_aha",
    "numbered_list",
    "behind_the_scenes",
    "5_sec_value",
    "social_proof",
)


# --------------------------------------------------------------------------- #
# StoryboardBeat (shared: s04_plan video_beats + s05_script storyboard)
# --------------------------------------------------------------------------- #


class StoryboardBeat(BaseModel):
    """One beat of the showcase storyboard.

    Used by ``MarketingBrief.video_beats`` (the 4-beat skeleton the planner
    drafts in ``s04_plan``) AND, in Slice 12, by ``Storyboard.beats`` (the
    fleshed-out script ``s05_script`` produces). A beat pairs a visual prompt
    with its voiceover line and an on-screen duration.

    Fields:
    * ``image_prompt`` — non-empty prompt describing the beat's visual (Imagen
      still or Veo conditioning).
    * ``narration`` — non-empty voiceover line for the beat.
    * ``duration_sec`` — on-screen seconds (> 0). ``s05_script`` constrains this
      to 3-5 s per beat; the brief skeleton only requires it be positive.
    """

    model_config = ConfigDict(extra="forbid")

    image_prompt: str
    narration: str
    duration_sec: float

    @field_validator("image_prompt", "narration")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("storyboard beat fields must be non-empty strings")
        return v

    @field_validator("duration_sec")
    @classmethod
    def _positive_duration(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("duration_sec must be > 0")
        return v


# --------------------------------------------------------------------------- #
# Storyboard (s05_script artifact)
# --------------------------------------------------------------------------- #


class Storyboard(BaseModel):
    """The ``05_script/storyboard.json`` artifact written by ``s05_script``.

    Produced by the ``demo-script-writer`` sub-agent (a single ``claude -p``
    call), which fleshes out the planner's 4-beat skeleton into the showcase
    storyboard: each beat pairs a visual prompt with its voiceover line and an
    on-screen duration.

    Beat-count rule (HARD): 4-6 beats inclusive. ``s05_script.run`` ALSO
    enforces this against the parsed sub-agent JSON BEFORE writing, raising
    :class:`shipcast.errors.SubagentMalformedOutput` for a count outside the
    range (TC-8.3 / TC-8.4). The same bound is duplicated here so the default
    ``validate_outputs`` re-checks it on disk as defense-in-depth.

    Duration rule (HARD): each beat's ``duration_sec`` MUST be in [3, 5]
    inclusive (showcase pacing — 4 beats x 3-5 s). ``StoryboardBeat`` already
    requires ``duration_sec > 0``; this model tightens it to the 3-5 s window.

    Fields:
    * ``beats`` — 4-6 :class:`StoryboardBeat` objects, each
      ``{image_prompt, narration, duration_sec}`` with ``duration_sec`` in 3-5 s.
    """

    model_config = ConfigDict(extra="forbid")

    beats: list[StoryboardBeat]

    @field_validator("beats")
    @classmethod
    def _beat_count_and_durations(
        cls, v: list[StoryboardBeat]
    ) -> list[StoryboardBeat]:
        if not (4 <= len(v) <= 6):
            raise ValueError(
                f"storyboard must contain 4-6 beats (inclusive), got {len(v)}"
            )
        for i, beat in enumerate(v):
            if not (3.0 <= beat.duration_sec <= 5.0):
                raise ValueError(
                    f"beat {i} duration_sec must be in [3, 5] s, "
                    f"got {beat.duration_sec}"
                )
        return v


# --------------------------------------------------------------------------- #
# VideoBeats (s06_video_assets artifact — the per-beat clip manifest)
# --------------------------------------------------------------------------- #


class VideoClip(BaseModel):
    """One rendered clip recorded in ``06_video_assets/clips.json``.

    Fields:
    * ``index`` — 0-based beat index (drives the ``beat_{index:02d}.mp4`` name).
    * ``filename`` — the clip filename relative to the stage dir.
    * ``source`` — how the clip was produced: ``"veo"`` (premium hero) or
      ``"ken_burns"`` (Imagen still + pan/zoom). A premium beat[0] that fell
      back from a Veo safety block records ``"ken_burns"`` so the manifest is
      an honest record of what was actually rendered.
    * ``duration_sec`` — the clip's target on-screen seconds.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    filename: str
    source: Literal["veo", "ken_burns"]
    duration_sec: float

    @field_validator("filename")
    @classmethod
    def _filename_safe(cls, v: str) -> str:
        if not v.strip() or "/" in v or "\\" in v or ".." in v:
            raise ValueError("clip filename must be a bare, safe filename")
        return v


class VideoBeats(BaseModel):
    """The ``06_video_assets/clips.json`` artifact written by ``s06_video_assets``.

    A small JSON manifest recording the render mode and one :class:`VideoClip`
    per beat. The MP4 files themselves are the load-bearing outputs; this JSON
    gives the stage a single schema-checkable artifact and lets downstream
    Stage 08 read clip ordering/sources without re-probing every file.

    Fields:
    * ``mode`` — ``"standard"`` or ``"premium"`` (the resolved render mode).
    * ``clips`` — the per-beat clips in order.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["standard", "premium"]
    clips: list[VideoClip]

    @field_validator("clips")
    @classmethod
    def _non_empty_clips(cls, v: list[VideoClip]) -> list[VideoClip]:
        if not v:
            raise ValueError("clips must contain at least one rendered clip")
        return v


# --------------------------------------------------------------------------- #
# CarouselBeat (s04_plan carousel_beats → s09_graphics LinkedIn carousel)
# --------------------------------------------------------------------------- #


class CarouselBeat(BaseModel):
    """One of the four interior beats of the LinkedIn document carousel.

    The 6-slide carousel (``s09_graphics``) maps as: slide 1 = the chosen hook,
    slides 2-5 = these FOUR ``carousel_beats``, slide 6 = a CTA. Fixing the beat
    count at exactly 4 means the carousel composer never pads or truncates.

    Fields:
    * ``headline`` — non-empty slide headline (large type).
    * ``body`` — supporting line(s) for the slide (may be empty for a
      headline-only slide).
    """

    model_config = ConfigDict(extra="forbid")

    headline: str
    body: str = ""

    @field_validator("headline")
    @classmethod
    def _headline_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("carousel beat headline must be a non-empty string")
        return v


# --------------------------------------------------------------------------- #
# MarketingBrief (s04_plan artifact)
# --------------------------------------------------------------------------- #


class MarketingBrief(BaseModel):
    """The ``04_plan/brief.json`` artifact written by ``s04_plan``.

    Produced by a CHAINED sub-agent invocation: the ``planner`` sub-agent drafts
    the brief, then the ``brand-guardian`` sub-agent consumes that draft and
    returns the final, voice/visual-conformant version (guardian's output wins).

    Length constraints are HARD (the carousel + video pipelines depend on them):
    * ``video_beats`` — EXACTLY 4 (one hero + three fill; drives ``s06`` /
      Veo-clip targeting).
    * ``carousel_beats`` — EXACTLY 4 (slide 1 = hook, slides 2-5 = these,
      slide 6 = CTA -> a clean 6-slide carousel with no padding).

    Fields:
    * ``hook_template_per_channel`` — one hook-catalog key per channel; every
      value MUST be one of :data:`HOOK_TEMPLATE_KEYS`.
    * ``ctas`` — at least one call-to-action string.
    * ``video_beats`` — the 4-beat showcase skeleton.
    * ``carousel_beats`` — the 4 interior carousel beats.
    * ``has_stat_card`` — whether ``s09_graphics`` should render the stat card.
    * ``has_code_screenshot`` — whether ``s09_graphics`` should render the
      code screenshot (Pygments/Ray.so).
    """

    model_config = ConfigDict(extra="forbid")

    hook_template_per_channel: dict[Literal["x", "linkedin", "blog"], str]
    ctas: list[str]
    video_beats: list[StoryboardBeat]
    carousel_beats: list[CarouselBeat]
    has_stat_card: bool
    has_code_screenshot: bool

    @field_validator("hook_template_per_channel")
    @classmethod
    def _hooks_in_catalog(
        cls, v: dict[str, str]
    ) -> dict[str, str]:
        for channel, key in v.items():
            if key not in HOOK_TEMPLATE_KEYS:
                raise ValueError(
                    f"hook_template_per_channel[{channel!r}]={key!r} is not a "
                    f"catalog key; allowed: {HOOK_TEMPLATE_KEYS}"
                )
        for required_channel in ("x", "linkedin", "blog"):
            if required_channel not in v:
                raise ValueError(
                    f"hook_template_per_channel must include channel "
                    f"{required_channel!r}"
                )
        return v

    @field_validator("ctas")
    @classmethod
    def _ctas_non_empty(cls, v: list[str]) -> list[str]:
        if not v or not any(c.strip() for c in v):
            raise ValueError("ctas must contain at least one non-empty string")
        return v

    @field_validator("video_beats")
    @classmethod
    def _exactly_four_video_beats(
        cls, v: list[StoryboardBeat]
    ) -> list[StoryboardBeat]:
        if len(v) != 4:
            raise ValueError(
                f"video_beats must contain EXACTLY 4 beats (1 hero + 3 fill), "
                f"got {len(v)}"
            )
        return v

    @field_validator("carousel_beats")
    @classmethod
    def _exactly_four_carousel_beats(
        cls, v: list[CarouselBeat]
    ) -> list[CarouselBeat]:
        if len(v) != 4:
            raise ValueError(
                f"carousel_beats must contain EXACTLY 4 beats (slides 2-5), "
                f"got {len(v)}"
            )
        return v


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _is_relative_to(path: Path, root: Path) -> bool:
    """``Path.is_relative_to`` without raising (3.12 has it, kept explicit)."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
