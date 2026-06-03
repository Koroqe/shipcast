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
        url = str(v)
        parts = urlsplit(url)

        # 1. Scheme check — MUST run before any DNS lookup.
        if parts.scheme != "https":
            raise ValueError(
                f"live_url must use the 'https' scheme, got {parts.scheme!r}"
            )

        host = parts.hostname
        if not host:
            raise ValueError("live_url must include a hostname")

        # 2. Resolve the hostname to EVERY address (IPv4 + IPv6) and reject if
        #    ANY of them is non-public. Using getaddrinfo (not gethostbyname,
        #    which is IPv4-only) closes the IPv6-only-AAAA SSRF gap and lets
        #    IPv6-literal hosts ([::1]) reject cleanly instead of raising an
        #    unhandled gaierror. Rejected ranges: RFC1918 private (10/8,
        #    172.16/12, 192.168/16), loopback (127/8, ::1), link-local
        #    (169.254/16, fe80::/10), unique-local IPv6 (fc00::/7 → is_private),
        #    plus 0.0.0.0/unspecified, reserved, and multicast — all of which
        #    are SSRF targets on at least one supported platform.
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
# Helpers
# --------------------------------------------------------------------------- #


def _is_relative_to(path: Path, root: Path) -> bool:
    """``Path.is_relative_to`` without raising (3.12 has it, kept explicit)."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
