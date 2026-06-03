"""Slice 3 — InputYaml / WalkthroughStep validation tests.

Covers TC-3.1 .. TC-3.17, TC-4.13, TC-19.6.

SSRF defense (resolved-IP check) and path-traversal defense (incl. symlink
escape) are exercised here. `socket.getaddrinfo` is always monkeypatched so
no real network call ever happens.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import shipcast.schemas as schemas
from shipcast.schemas import InputYaml, WalkthroughStep

# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


def _addrinfo(ip: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Build a minimal getaddrinfo() return value resolving to `ip`."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.fixture
def public_ip_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every hostname to a public IP so the SSRF gate passes."""

    def _resolve(host: str, *args: object, **kwargs: object) -> object:
        return _addrinfo("93.184.216.34")  # example.com, public

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


@pytest.fixture
def no_dns_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any DNS lookup explode, to prove scheme rejection fires first."""

    def _boom(host: str, *args: object, **kwargs: object) -> object:
        raise AssertionError(
            f"socket.getaddrinfo was called for {host!r}; scheme check "
            "should have rejected before any DNS lookup"
        )

    monkeypatch.setattr(socket, "getaddrinfo", _boom)


@pytest.fixture
def allowed_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Repoint the allowed-root constant at a writable tmp dir.

    The production default stays `/Users/aleksei/Documents/Projects.nosync/`;
    only the accept/symlink tests monkeypatch it to `tmp_path` so they can
    create real directories under the allowed root.
    """
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", tmp_path.resolve())
    return tmp_path


def _valid_repo(root: Path) -> Path:
    """A real directory under `root` containing a CHANGELOG.md."""
    repo = root / "getdeal-platform-monorepo"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text("# Changelog\n")
    return repo


def _base_kwargs(repo: Path, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "repo_path": str(repo),
        "entry_heading": "Add CSV export",
        "live_url": "https://example.com",
        "brand_slug": "getdeal",
        "video_mode": "standard",
    }
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------- #
# Rejections — URL scheme (no DNS, TC-3.1 / TC-3.2 / TC-3.17)
# --------------------------------------------------------------------------- #


def test_reject_http_scheme(
    allowed_root: Path, no_dns_resolver: None
) -> None:
    """TC-3.1 — http:// rejected; DNS never called (no_dns_resolver would raise)."""
    repo = _valid_repo(allowed_root)
    with pytest.raises(ValidationError) as exc:
        InputYaml(**_base_kwargs(repo, live_url="http://example.com"))
    assert "live_url" in str(exc.value)


def test_reject_ftp_scheme(allowed_root: Path, no_dns_resolver: None) -> None:
    """TC-3.2 — ftp:// rejected before hostname resolution."""
    repo = _valid_repo(allowed_root)
    with pytest.raises(ValidationError):
        InputYaml(**_base_kwargs(repo, live_url="ftp://example.com/file"))


def test_scheme_rejection_fires_before_dns(
    allowed_root: Path, no_dns_resolver: None
) -> None:
    """TC-3.17 — bad-scheme URLs raise ValidationError, not AssertionError."""
    repo = _valid_repo(allowed_root)
    for bad in ("http://example.com", "ftp://example.com"):
        with pytest.raises(ValidationError):
            InputYaml(**_base_kwargs(repo, live_url=bad))


# --------------------------------------------------------------------------- #
# Rejections — resolved private/loopback/link-local IPs (TC-3.3 .. TC-3.7)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "resolved_ip",
    [
        "192.168.1.1",  # TC-3.3 RFC1918 192.168/16
        "10.0.0.5",  # TC-3.4 RFC1918 10/8
        "172.16.0.1",  # TC-3.5 RFC1918 172.16/12
        "127.0.0.1",  # TC-3.6 loopback
        "169.254.1.1",  # TC-3.7 link-local
        "0.0.0.0",  # MAJOR-1 unspecified (SSRF target on Linux)
        "240.0.0.1",  # MAJOR-1 reserved (240.0.0.0/4)
        "::1",  # MAJOR-1 IPv6 loopback (via AAAA-only record)
        "fd00::1",  # MAJOR-1 IPv6 unique-local (is_private)
        "fe80::1",  # MAJOR-1 IPv6 link-local
    ],
)
def test_reject_private_resolved_ip(
    allowed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_ip: str,
) -> None:
    repo = _valid_repo(allowed_root)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: _addrinfo(resolved_ip)
    )
    with pytest.raises(ValidationError):
        InputYaml(**_base_kwargs(repo, live_url="https://internal.example.com"))


def test_reject_unresolvable_host_cleanly(
    allowed_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-1 — a hostname that fails to resolve raises ValidationError, not gaierror."""

    def _boom(host: str, *a: object, **k: object) -> object:
        raise socket.gaierror("Name or service not known")

    repo = _valid_repo(allowed_root)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(ValidationError):
        InputYaml(**_base_kwargs(repo, live_url="https://nonexistent.invalid"))


# --------------------------------------------------------------------------- #
# Rejections — repo_path (TC-3.8 .. TC-3.10, TC-19.6)
# --------------------------------------------------------------------------- #


def test_reject_repo_path_with_dotdot(public_ip_resolver: None) -> None:
    """TC-3.8 — `..` segment rejected without filesystem access."""
    with pytest.raises(ValidationError):
        InputYaml(
            repo_path="/Users/aleksei/Documents/Projects.nosync/../etc/passwd",
            entry_heading="Add CSV export",
            live_url="https://example.com",
            brand_slug="getdeal",
            video_mode="standard",
        )


def test_reject_repo_path_outside_allowed_root(
    public_ip_resolver: None,
) -> None:
    """TC-3.9 — path outside the allowed root rejected."""
    with pytest.raises(ValidationError):
        InputYaml(
            repo_path="/tmp/some-other-project",
            entry_heading="Add CSV export",
            live_url="https://example.com",
            brand_slug="getdeal",
            video_mode="standard",
        )


def test_reject_repo_path_without_changelog(
    allowed_root: Path, public_ip_resolver: None
) -> None:
    """TC-3.10 — directory under allowed root but missing CHANGELOG.md."""
    repo = allowed_root / "no-changelog"
    repo.mkdir()
    with pytest.raises(ValidationError):
        InputYaml(**_base_kwargs(repo))


def test_reject_symlink_escape(
    allowed_root: Path,
    tmp_path: Path,
    public_ip_resolver: None,
) -> None:
    """TC-19.6 — a symlink under the allowed root pointing outside is rejected.

    The escape target is a real repo (with CHANGELOG.md) located OUTSIDE the
    allowed root; only resolving the symlink reveals the escape, so the
    post-resolution re-check is what must fire.
    """
    outside = tmp_path.parent / "outside-root-target"
    outside.mkdir()
    (outside / "CHANGELOG.md").write_text("# Changelog\n")
    link = allowed_root / "sneaky"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValidationError):
        InputYaml(**_base_kwargs(link))


# --------------------------------------------------------------------------- #
# Rejections — WalkthroughStep (TC-3.11 .. TC-3.13)
# --------------------------------------------------------------------------- #


def test_reject_unknown_walkthrough_action() -> None:
    """TC-3.11 — action not in the allowed literal set."""
    with pytest.raises(ValidationError):
        WalkthroughStep(action="eval", selector="#btn")


def test_reject_javascript_selector() -> None:
    """TC-3.12 — selector containing javascript: scheme rejected."""
    with pytest.raises(ValidationError):
        WalkthroughStep(action="click", selector="javascript:alert(1)")


def test_reject_missing_required_field() -> None:
    """TC-3.13 — missing `action` (required) raises."""
    with pytest.raises(ValidationError):
        WalkthroughStep.model_validate({"selector": "#btn"})


# --------------------------------------------------------------------------- #
# Rejections — InputYaml-level required / literal (TC-3.16, TC-4.13)
# --------------------------------------------------------------------------- #


def test_reject_bad_video_mode(
    allowed_root: Path, public_ip_resolver: None
) -> None:
    """TC-3.16 — video_mode='veo3' rejected."""
    repo = _valid_repo(allowed_root)
    with pytest.raises(ValidationError):
        InputYaml(**_base_kwargs(repo, video_mode="veo3"))


def test_reject_empty_entry_heading(
    allowed_root: Path, public_ip_resolver: None
) -> None:
    """TC-4.13 — empty entry_heading rejected (min_length=1)."""
    repo = _valid_repo(allowed_root)
    with pytest.raises(ValidationError):
        InputYaml(**_base_kwargs(repo, entry_heading=""))


def test_reject_missing_required_input_field(
    allowed_root: Path, public_ip_resolver: None
) -> None:
    """TC-3.13 (InputYaml flavor) — missing brand_slug raises."""
    repo = _valid_repo(allowed_root)
    kwargs = _base_kwargs(repo)
    del kwargs["brand_slug"]
    with pytest.raises(ValidationError):
        InputYaml(**kwargs)


# --------------------------------------------------------------------------- #
# Accepts (TC-3.14 / TC-3.15)
# --------------------------------------------------------------------------- #


def test_accept_valid_standard(
    allowed_root: Path, public_ip_resolver: None
) -> None:
    """TC-3.14 — valid standard-mode input parses cleanly."""
    repo = _valid_repo(allowed_root)
    model = InputYaml(
        **_base_kwargs(
            repo,
            feature_walkthrough=[
                {"action": "goto", "value": "https://example.com"},
                {"action": "click", "selector": "#cta"},
            ],
        )
    )
    assert model.video_mode == "standard"
    assert model.brand_slug == "getdeal"
    assert model.feature_walkthrough is not None
    assert len(model.feature_walkthrough) == 2


def test_accept_valid_premium(
    allowed_root: Path, public_ip_resolver: None
) -> None:
    """TC-3.15 — valid premium-mode input parses cleanly."""
    repo = _valid_repo(allowed_root)
    model = InputYaml(**_base_kwargs(repo, video_mode="premium"))
    assert model.video_mode == "premium"


def test_accept_no_live_url(
    allowed_root: Path, no_dns_resolver: None
) -> None:
    """live_url is optional; absent → no DNS lookup at all."""
    repo = _valid_repo(allowed_root)
    model = InputYaml(**_base_kwargs(repo, live_url=None))
    assert model.live_url is None


# --------------------------------------------------------------------------- #
# Production default sanity — allowed root must remain the real prod path
# --------------------------------------------------------------------------- #


def test_production_allowed_root_unchanged() -> None:
    """The production default must not be the tmp monkeypatch leaking out."""
    assert (
        str(schemas.ALLOWED_REPO_ROOT)
        == "/Users/aleksei/Documents/Projects.nosync"
    )
