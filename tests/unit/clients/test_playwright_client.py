"""Slice 8 — `PlaywrightClient` unit tests.

The concrete Playwright wrapper used by ``s02_enrich`` (screenshots) and
``s03_brand`` (palette / font / logo extraction). These tests NEVER launch a
real browser or touch the network — the Playwright transport is injected via a
``page_factory`` seam, and ``socket.getaddrinfo`` (the SSRF DNS lookup) is
monkeypatched.

Security focus (this slice is security-pre-review flagged):

* The shared ``schemas.validate_live_url`` SSRF validator MUST run BEFORE any
  navigation on EVERY navigating method. Proven by a page factory that raises
  ``AssertionError`` if it is ever reached with a bad URL — a passing test means
  the validator rejected first.
* A 60 s navigation timeout overrun raises ``PlaywrightTimeout``.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import shipcast.schemas as schemas
from shipcast.clients.playwright_client import (
    NAV_TIMEOUT_MS,
    PlaywrightClient,
)
from shipcast.errors import PlaywrightTimeout

# --------------------------------------------------------------------------- #
# DNS resolver helpers (no real network)
# --------------------------------------------------------------------------- #


def _addrinfo(ip: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to a public IP so the SSRF gate passes."""
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: _addrinfo("93.184.216.34")
    )


@pytest.fixture
def private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to an RFC1918 address so the SSRF gate rejects."""
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: _addrinfo("192.168.1.1")
    )


# --------------------------------------------------------------------------- #
# Fake Playwright transport (a "page-like" object)
# --------------------------------------------------------------------------- #


class _FakePage:
    """Minimal stand-in for a Playwright Page used by PlaywrightClient.

    Records navigations and returns canned extraction results. A PNG screenshot
    is a valid 1x1 PNG so callers that inspect bytes/headers are satisfied.
    """

    _PNG_1X1 = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def __init__(
        self,
        *,
        colors: list[str] | None = None,
        font: str = "Inter, sans-serif",
        logo: bytes | None = None,
    ) -> None:
        self.gotos: list[str] = []
        self.eval_calls: list[str] = []
        self.actions: list[tuple[str, Any]] = []
        self._colors = colors or ["#112233", "#445566", "#778899"]
        self._font = font
        self._logo = logo if logo is not None else self._PNG_1X1
        self.closed = False

    # -- navigation ---------------------------------------------------------
    def goto(self, url: str, *, timeout: float | None = None) -> None:
        self.gotos.append(url)

    # -- extraction ---------------------------------------------------------
    def computed_colors(self) -> list[str]:
        self.eval_calls.append("colors")
        return self._colors

    def font_family(self) -> str:
        self.eval_calls.append("font")
        return self._font

    def logo_bytes(self) -> bytes | None:
        self.eval_calls.append("logo")
        return self._logo

    # -- walkthrough actions ------------------------------------------------
    def click(self, selector: str) -> None:
        self.actions.append(("click", selector))

    def fill(self, selector: str, value: str) -> None:
        self.actions.append(("type", (selector, value)))

    def wait(self, ms: int) -> None:
        self.actions.append(("wait", ms))

    def screenshot_png(self) -> bytes:
        self.actions.append(("screenshot", None))
        return self._PNG_1X1

    def close(self) -> None:
        self.closed = True


class _ExplodingPage(_FakePage):
    """A page whose construction is forbidden — proves the validator ran first."""

    def __init__(self, *a: Any, **k: Any) -> None:  # pragma: no cover - guard
        raise AssertionError(
            "page factory reached for a URL that should have been rejected "
            "by validate_live_url BEFORE any navigation"
        )


def _factory(page: _FakePage) -> Any:
    """A page_factory that yields the given fake page (ignores the URL)."""

    def _make(url: str) -> _FakePage:
        page.goto(url, timeout=NAV_TIMEOUT_MS)
        return page

    return _make


# --------------------------------------------------------------------------- #
# Shape tests — each method returns the expected type
# --------------------------------------------------------------------------- #

URL = "https://example.com/feature"


def test_extract_css_palette_returns_hex_list(public_dns: None) -> None:
    page = _FakePage(colors=["#aa0011", "#00bb22", "#0000cc", "#ddeeff", "#102030"])
    client = PlaywrightClient(page_factory=_factory(page))
    palette = client.extract_css_palette(URL)
    assert isinstance(palette, list)
    assert palette and all(c.startswith("#") for c in palette)
    assert len(palette) <= 5
    assert page.gotos == [URL]


def test_extract_font_family_returns_str(public_dns: None) -> None:
    page = _FakePage(font="Inter, Arial, sans-serif")
    client = PlaywrightClient(page_factory=_factory(page))
    font = client.extract_font_family(URL)
    assert isinstance(font, str)
    assert font == "Inter, Arial, sans-serif"
    assert page.gotos == [URL]


def test_screenshot_logo_returns_bytes(public_dns: None) -> None:
    page = _FakePage()
    client = PlaywrightClient(page_factory=_factory(page))
    logo = client.screenshot_logo(URL)
    assert isinstance(logo, (bytes, bytearray))
    assert logo[:4] == b"\x89PNG"


def test_screenshot_page_returns_png_bytes(public_dns: None) -> None:
    """First-screen screenshot routes through the page and returns PNG bytes."""
    page = _FakePage()
    client = PlaywrightClient(page_factory=_factory(page))
    data = client.screenshot_page(URL)
    assert isinstance(data, (bytes, bytearray))
    assert data[:4] == b"\x89PNG"
    assert page.gotos == [URL]
    assert page.closed is True


def test_screenshot_page_private_url_rejected_before_navigation(
    private_dns: None,
) -> None:
    """RFC1918-resolving URL → ValidationError; page factory never reached."""
    client = PlaywrightClient(page_factory=_ExplodingPage)
    with pytest.raises(ValidationError):
        client.screenshot_page("https://internal.example.com/")


def test_screenshot_page_timeout_raises_playwright_timeout(
    public_dns: None,
) -> None:
    client = PlaywrightClient(page_factory=_timeout_factory)
    with pytest.raises(PlaywrightTimeout):
        client.screenshot_page(URL)


def test_screenshot_logo_returns_none_when_absent(public_dns: None) -> None:
    page = _FakePage(logo=None)
    # Pass logo=None explicitly via a page that returns None.
    page._logo = None  # type: ignore[assignment]
    client = PlaywrightClient(page_factory=_factory(page))
    assert client.screenshot_logo(URL) is None


def test_screenshot_feature_writes_pngs(public_dns: None, tmp_path: Path) -> None:
    page = _FakePage()
    client = PlaywrightClient(page_factory=_factory(page), output_dir=tmp_path)
    walkthrough = [
        {"action": "click", "selector": "#start"},
        {"action": "screenshot"},
        {"action": "type", "selector": "#q", "value": "hello"},
        {"action": "screenshot"},
    ]
    paths = client.screenshot_feature(URL, walkthrough)
    assert isinstance(paths, list)
    assert len(paths) == 2
    for p in paths:
        assert isinstance(p, Path)
        assert p.exists()
        assert p.read_bytes()[:4] == b"\x89PNG"


def test_screenshot_feature_empty_walkthrough_takes_viewport(
    public_dns: None, tmp_path: Path
) -> None:
    """No explicit screenshot step → a single viewport screenshot (UC-3-A2)."""
    page = _FakePage()
    client = PlaywrightClient(page_factory=_factory(page), output_dir=tmp_path)
    paths = client.screenshot_feature(URL, [])
    assert len(paths) == 1
    assert paths[0].read_bytes()[:4] == b"\x89PNG"


# --------------------------------------------------------------------------- #
# SECURITY — URL validator runs BEFORE any navigation, on every method
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method",
    ["extract_css_palette", "extract_font_family", "screenshot_logo"],
)
def test_private_url_rejected_before_navigation(
    private_dns: None, method: str
) -> None:
    """RFC1918-resolving URL → ValidationError; page factory never reached."""
    client = PlaywrightClient(page_factory=_ExplodingPage)
    with pytest.raises(ValidationError):
        getattr(client, method)("https://internal.example.com/")


def test_screenshot_feature_private_url_rejected_before_navigation(
    private_dns: None, tmp_path: Path
) -> None:
    client = PlaywrightClient(page_factory=_ExplodingPage, output_dir=tmp_path)
    with pytest.raises(ValidationError):
        client.screenshot_feature("https://internal.example.com/", [])


@pytest.mark.parametrize(
    "method",
    ["extract_css_palette", "extract_font_family", "screenshot_logo"],
)
def test_non_https_url_rejected_before_navigation(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """http:// scheme → rejected before any DNS lookup or navigation."""

    def _boom(host: str, *a: object, **k: object) -> object:
        raise AssertionError("getaddrinfo called — scheme check should fire first")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    client = PlaywrightClient(page_factory=_ExplodingPage)
    with pytest.raises(ValidationError):
        getattr(client, method)("http://example.com/")


def test_loopback_url_rejected_before_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: _addrinfo("127.0.0.1")
    )
    client = PlaywrightClient(page_factory=_ExplodingPage)
    with pytest.raises(ValidationError):
        client.extract_css_palette("https://localhost.example.com/")


def test_link_local_url_rejected_before_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: _addrinfo("169.254.1.1")
    )
    client = PlaywrightClient(page_factory=_ExplodingPage)
    with pytest.raises(ValidationError):
        client.extract_font_family("https://metadata.example.com/")


def test_validator_delegates_to_shared_schema_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client uses schemas.validate_live_url (single source of truth)."""
    calls: list[str] = []
    real = schemas.validate_live_url

    def _spy(url: str) -> None:
        calls.append(url)
        real(url)

    monkeypatch.setattr(
        "shipcast.clients.playwright_client.validate_live_url", _spy
    )
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: _addrinfo("93.184.216.34")
    )
    page = _FakePage()
    client = PlaywrightClient(page_factory=_factory(page))
    client.extract_css_palette(URL)
    assert calls == [URL]


# --------------------------------------------------------------------------- #
# SECURITY — navigation timeout raises PlaywrightTimeout
# --------------------------------------------------------------------------- #


class _TimeoutPage(_FakePage):
    def goto(self, url: str, *, timeout: float | None = None) -> None:
        from shipcast.clients.playwright_client import _PlaywrightTimeoutError

        raise _PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded navigating {url}")


def _timeout_factory(url: str) -> _FakePage:
    page = _TimeoutPage()
    page.goto(url, timeout=NAV_TIMEOUT_MS)
    return page


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.extract_css_palette(URL),
        lambda c: c.extract_font_family(URL),
        lambda c: c.screenshot_logo(URL),
    ],
)
def test_navigation_timeout_raises_playwright_timeout(
    public_dns: None, call: Any
) -> None:
    client = PlaywrightClient(page_factory=_timeout_factory)
    with pytest.raises(PlaywrightTimeout):
        call(client)


def test_screenshot_feature_timeout_raises_playwright_timeout(
    public_dns: None, tmp_path: Path
) -> None:
    client = PlaywrightClient(page_factory=_timeout_factory, output_dir=tmp_path)
    with pytest.raises(PlaywrightTimeout):
        client.screenshot_feature(URL, [])


def test_nav_timeout_is_60s() -> None:
    """60 s navigation budget (plan: '60 s nav timeout')."""
    assert NAV_TIMEOUT_MS == 60_000


# --------------------------------------------------------------------------- #
# Pure helpers — palette/hex normalization + timeout classification
# --------------------------------------------------------------------------- #


def test_palette_normalizes_rgb_and_dedupes(public_dns: None) -> None:
    page = _FakePage(
        colors=[
            "rgb(255, 0, 0)",          # -> #ff0000
            "#ff0000",                  # dup -> dropped
            "rgba(0, 128, 255, 0.5)",  # -> #0080ff
            "#abc",                     # shorthand -> #aabbcc
            "transparent",              # dropped (not a hex/rgb)
            "rgb(1,2)",                 # malformed -> dropped
            "#00ff00",
            "#0000ff",
            "#123456",                  # 6th -> capped out
        ],
    )
    client = PlaywrightClient(page_factory=_factory(page))
    palette = client.extract_css_palette(URL)
    assert palette == ["#ff0000", "#0080ff", "#aabbcc", "#00ff00", "#0000ff"]


def test_is_playwright_timeout_matches_by_name_and_module() -> None:
    from shipcast.clients.playwright_client import _is_playwright_timeout

    class _PWTimeout(Exception):
        pass

    _PWTimeout.__module__ = "playwright._impl._errors"
    _PWTimeout.__name__ = "TimeoutError"
    assert _is_playwright_timeout(_PWTimeout())
    # A stdlib TimeoutError (wrong module) is NOT classified as a PW timeout.
    assert not _is_playwright_timeout(TimeoutError())


def test_screenshot_logo_real_path_smoke() -> None:
    """The default real factory imports playwright lazily — never at module top.

    We don't launch a browser here; we only assert the factory is callable and
    the SDK import is deferred (covered by the deferred-import test). This guard
    documents that ``_default_page_factory`` is the production seam.
    """
    from shipcast.clients import playwright_client as mod

    assert callable(mod._default_page_factory)
