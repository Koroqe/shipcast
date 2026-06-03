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
# Helpers
# --------------------------------------------------------------------------- #


def _is_relative_to(path: Path, root: Path) -> bool:
    """``Path.is_relative_to`` without raising (3.12 has it, kept explicit)."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
