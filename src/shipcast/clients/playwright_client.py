"""Playwright wrapper for live-app brand extraction + feature screenshots.

Slice 8. Used by:

* ``s02_enrich`` — replays the operator's ``feature_walkthrough`` against the
  live app and captures ``02_enrich/screenshots/*.png`` (duck-typed
  ``screenshot_feature``).
* ``s03_brand`` (Slice 10) — extracts the CSS palette, font family, and logo
  from the live app (``extract_css_palette`` / ``extract_font_family`` /
  ``screenshot_logo``).

Security (this client is security-pre-review flagged)
-----------------------------------------------------
EVERY navigating method calls :func:`shipcast.schemas.validate_live_url` BEFORE
the browser is touched. ``validate_live_url`` is the SAME SSRF defense the
``InputYaml.live_url`` field validator delegates to (https-only; resolved-IP
private / loopback / link-local / unspecified / reserved / multicast rejection,
IPv4 + IPv6). A rejected URL raises ``ValueError`` (wrapped as a Pydantic-style
``ValidationError`` so callers see the same type as input.yaml validation) and
the page factory is never reached.

A 60 s navigation timeout (:data:`NAV_TIMEOUT_MS`) is enforced on every
``goto``; an overrun is normalized to :class:`~shipcast.errors.PlaywrightTimeout`.

Lazy-import discipline
----------------------
The heavy ``playwright`` SDK is imported INSIDE the default page factory only —
never at module top — so importing this module (or any transitive import from
``shipcast.cli``) does NOT pull ``playwright`` into ``sys.modules``. Stage tests
inject a ``page_factory`` that yields a fake page, so no real browser launches.
This mirrors the Gemini/ffmpeg/elevenlabs lazy-client invariant.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import ValidationError

from shipcast.errors import PlaywrightTimeout
from shipcast.schemas import validate_live_url

if TYPE_CHECKING:
    from collections.abc import Callable

#: Navigation timeout in milliseconds (60 s — plan: "60 s nav timeout").
NAV_TIMEOUT_MS: int = 60_000

#: Walkthrough actions Playwright is permitted to perform (mirrors
#: ``schemas.WalkthroughStep`` — defense in depth against an un-validated step).
_ALLOWED_ACTIONS = frozenset({"goto", "click", "type", "wait", "screenshot"})


class _PlaywrightTimeoutError(Exception):
    """Local timeout marker.

    The default factory raises Playwright's real ``TimeoutError`` on a slow
    ``goto``; this client normalizes BOTH that real error and this local marker
    (which tests raise to avoid importing the SDK) into
    :class:`~shipcast.errors.PlaywrightTimeout`.
    """


# --------------------------------------------------------------------------- #
# Page protocol (structural; the real adapter + test fakes both satisfy it)
# --------------------------------------------------------------------------- #


@runtime_checkable
class _PageLike(Protocol):
    """The minimal page surface ``PlaywrightClient`` drives.

    The default factory returns an adapter wrapping a real Playwright page that
    has already navigated to the (validated) URL. Tests inject a fake.
    """

    def computed_colors(self) -> list[str]: ...
    def font_family(self) -> str: ...
    def logo_bytes(self) -> bytes | None: ...
    def click(self, selector: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def wait(self, ms: int) -> None: ...
    def screenshot_png(self) -> bytes: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Default (real) page factory — heavy import lives HERE, inside the closure.
# --------------------------------------------------------------------------- #


def _default_page_factory(url: str) -> _PageLike:  # pragma: no cover - real browser
    """Launch headless Chromium, navigate to ``url`` (already validated), adapt.

    ``playwright`` is imported here — NOT at module top — to preserve the lazy
    import-purity invariant. The caller has ALREADY run ``validate_live_url`` on
    ``url`` before reaching this factory.
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="networkidle")

    class _RealPage:
        def computed_colors(self) -> list[str]:
            result = page.evaluate(_PALETTE_JS)
            return [str(c) for c in result] if isinstance(result, list) else []

        def font_family(self) -> str:
            return str(page.evaluate(_FONT_JS))

        def logo_bytes(self) -> bytes | None:
            locator = page.locator(_LOGO_SELECTOR).first
            if locator.count() == 0:
                return None
            try:
                return bytes(locator.screenshot())
            except Exception:
                return None

        def click(self, selector: str) -> None:
            page.click(selector, timeout=NAV_TIMEOUT_MS)

        def fill(self, selector: str, value: str) -> None:
            page.fill(selector, value, timeout=NAV_TIMEOUT_MS)

        def wait(self, ms: int) -> None:
            page.wait_for_timeout(ms)

        def screenshot_png(self) -> bytes:
            return bytes(page.screenshot(type="png"))

        def close(self) -> None:
            browser.close()
            pw.stop()

    return _RealPage()


#: JS that collects visible computed colors (background + text) across the DOM.
_PALETTE_JS = """
() => {
  const counts = {};
  for (const el of document.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    for (const c of [s.backgroundColor, s.color]) {
      if (!c || c === 'rgba(0, 0, 0, 0)' || c === 'transparent') continue;
      counts[c] = (counts[c] || 0) + 1;
    }
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1]).map(e => e[0]);
}
"""

_FONT_JS = "() => getComputedStyle(document.body).fontFamily"

_LOGO_SELECTOR = (
    "img[alt*='logo' i], img[src*='logo' i], a[href='/'] img, header img, "
    "svg[aria-label*='logo' i]"
)


# --------------------------------------------------------------------------- #
# PlaywrightClient
# --------------------------------------------------------------------------- #


class PlaywrightClient:
    """Headless-browser extraction client. Construct inside ``stage.run()``.

    Parameters
    ----------
    page_factory:
        Callable ``(validated_url) -> _PageLike`` that performs navigation and
        returns a driveable page. Defaults to the real Chromium-backed factory;
        tests inject a fake so no browser launches.
    output_dir:
        Directory screenshots are written to (``screenshot_feature``). Defaults
        to a ``.screenshots`` dir under CWD; ``s02_enrich`` passes its stage dir.
    """

    def __init__(
        self,
        *,
        page_factory: Callable[[str], _PageLike] | None = None,
        output_dir: Path | None = None,
    ) -> None:
        self._page_factory: Callable[[str], _PageLike] = (
            page_factory or _default_page_factory
        )
        self._output_dir = output_dir

    def __repr__(self) -> str:
        return "<PlaywrightClient>"

    # ------------------------------------------------------------- navigation
    def _open(self, url: str) -> _PageLike:
        """Validate ``url`` (SSRF defense) THEN navigate. Order is load-bearing.

        ``validate_live_url`` runs FIRST — a rejected URL raises before the page
        factory (and therefore the browser) is ever touched. A navigation
        timeout from the factory is normalized to ``PlaywrightTimeout``.
        """
        # SECURITY: SSRF validation BEFORE any navigation, on every code path.
        try:
            validate_live_url(url)
        except ValueError as exc:
            # Surface as ValidationError so callers see the same type as the
            # input.yaml live_url validator (single source of truth).
            raise ValidationError.from_exception_data(
                "live_url",
                [{"type": "value_error", "loc": ("live_url",), "input": url,
                  "ctx": {"error": str(exc)}}],
            ) from exc

        try:
            return self._page_factory(url)
        except _PlaywrightTimeoutError as exc:
            raise PlaywrightTimeout(
                f"navigation to {url} exceeded {NAV_TIMEOUT_MS}ms"
            ) from exc
        except Exception as exc:
            if _is_playwright_timeout(exc):
                raise PlaywrightTimeout(
                    f"navigation to {url} exceeded {NAV_TIMEOUT_MS}ms"
                ) from exc
            raise

    # ------------------------------------------------------------- extraction
    def extract_css_palette(self, url: str) -> list[str]:
        """Return the top (≤5) computed colors as hex strings."""
        page = self._open(url)
        try:
            colors = page.computed_colors()
        finally:
            page.close()
        return _to_hex_palette(colors)

    def extract_font_family(self, url: str) -> str:
        """Return the live app's body ``font-family`` string."""
        page = self._open(url)
        try:
            return page.font_family()
        finally:
            page.close()

    def screenshot_logo(self, url: str) -> bytes | None:
        """Return the detected logo PNG bytes, or ``None`` if no logo found."""
        page = self._open(url)
        try:
            return page.logo_bytes()
        finally:
            page.close()

    # ------------------------------------------------------------- walkthrough
    def screenshot_feature(
        self, url: str, walkthrough: list[dict[str, object]]
    ) -> list[Path]:
        """Replay ``walkthrough`` against ``url`` and save PNGs; return paths.

        Each ``{"action": "screenshot"}`` step captures a PNG. If the walkthrough
        contains NO screenshot step (or is empty), a single viewport screenshot
        is captured after the steps run (UC-3-A2). Unknown actions are ignored
        (defense in depth — the schema already rejects them upstream).
        """
        page = self._open(url)
        out_dir = self._output_dir or (Path.cwd() / ".screenshots")
        out_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        try:
            took_explicit = False
            for step in walkthrough:
                action = str(step.get("action", ""))
                if action not in _ALLOWED_ACTIONS:
                    continue
                if action == "click" and step.get("selector"):
                    page.click(str(step["selector"]))
                elif action == "type" and step.get("selector"):
                    page.fill(str(step["selector"]), str(step.get("value", "")))
                elif action == "wait":
                    raw = step.get("value")
                    page.wait(int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else 1000)
                elif action == "screenshot":
                    saved.append(self._save_png(out_dir, page.screenshot_png(), len(saved)))
                    took_explicit = True
            if not took_explicit:
                saved.append(self._save_png(out_dir, page.screenshot_png(), len(saved)))
        finally:
            page.close()
        return saved

    @staticmethod
    def _save_png(out_dir: Path, data: bytes, index: int) -> Path:
        dest = out_dir / f"feature_{index:02d}.png"
        dest.write_bytes(data)
        return dest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _is_playwright_timeout(exc: BaseException) -> bool:
    """True if ``exc`` is Playwright's ``TimeoutError`` (matched by name).

    Matched by class name so we do not import the SDK to classify the error
    (the SDK may not even be loaded when a test injects a different timeout).
    """
    for klass in type(exc).__mro__:
        if klass.__name__ == "TimeoutError" and "playwright" in (
            getattr(klass, "__module__", "") or ""
        ):
            return True
    return False


def _to_hex_palette(colors: list[str]) -> list[str]:
    """Normalize CSS color strings to ``#rrggbb`` hex, dedupe, cap at 5."""
    out: list[str] = []
    seen: set[str] = set()
    for c in colors:
        hex_c = _css_color_to_hex(c)
        if hex_c is None or hex_c in seen:
            continue
        seen.add(hex_c)
        out.append(hex_c)
        if len(out) >= 5:
            break
    return out


def _css_color_to_hex(value: str) -> str | None:
    """Convert a ``#rrggbb`` or ``rgb()/rgba()`` color to ``#rrggbb``."""
    v = value.strip().lower()
    if v.startswith("#"):
        if len(v) == 7:
            return v
        if len(v) == 4:  # #rgb shorthand
            return "#" + "".join(ch * 2 for ch in v[1:])
        return None
    if v.startswith(("rgb(", "rgba(")):
        inner = v[v.index("(") + 1 : v.rindex(")")]
        nums = inner.replace("/", ",").split(",")
        try:
            r, g, b = (round(float(n.strip().rstrip("%"))) for n in nums[:3])
        except ValueError:
            return None
        if all(0 <= ch <= 255 for ch in (r, g, b)):
            return f"#{r:02x}{g:02x}{b:02x}"
    return None
