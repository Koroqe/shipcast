"""Gemini API client — image generation (Stage 06/09) + multimodal narrative (Stage 02).

Constructed lazily inside `stage.run()` only. The heavy ``requests`` import
lives INSIDE each method (not at module top), so importing this module — or any
transitive import from `cli.py` / `shipcast.stages` — does NOT pull `requests`
into ``sys.modules``. This preserves the Slice-1 import-purity invariant
(`tests/unit/test_package_imports.py` greps `sys.modules` for the `requests`
prefix after `import shipcast.cli`). Auth is via the operator's
``GEMINI_API_KEY`` SecretStr carried through `Settings`, passed as the
``x-goog-api-key`` header on every request.

Two surfaces, both single-HTTP-POST-per-call:

* ``generate_image`` — Stage 06/09 image generation. The retry loop lives in
  the OWNING stage (architect Ruling 7); the client raises
  :class:`GeminiTransientError` (429/5xx) / :class:`GeminiNonTransientError`
  (4xx + safety block) so the stage classifies in one place.
* ``multimodal`` — Stage 02 (`s02_enrich`) narrative generation. Posts a text
  prompt plus zero-or-more inline image parts to the Gemini 2.5 Pro
  ``generateContent`` surface and returns the model's text. There is NO
  stage-owned retry loop for this call (one shot per enrich run), so an HTTP
  429 surfaces as :class:`GeminiRateLimited` — a terminal failure the
  dispatcher records via the FAILED transition (TC-5.7).

NOTE: the ``aspect_ratio`` image parameter is intentionally NOT present yet —
it lands in Slice 9. This module keeps image generation at the fixed 16:9 / 2K
config until then.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from pydantic import SecretStr

from shipcast.errors import (
    GeminiNonTransientError,
    GeminiRateLimited,
    GeminiTransientError,
    MissingApiKey,
)

if TYPE_CHECKING:
    from pathlib import Path

#: AI Studio host. Hardcoded to the v1beta surface — the only public surface
#: that hosts the image-preview + 2.5-pro multimodal models today.
_API_BASE: Final[str] = "https://generativelanguage.googleapis.com/v1beta"

#: HTTP timeout per request (seconds). Generous because Gemini's surfaces are
#: slower than plain text. Transient timeouts on image calls are retried by the
#: owning stage; the multimodal call has no retry loop.
_HTTP_TIMEOUT_SEC: Final[float] = 120.0

#: HTTP status codes that warrant a retry on the IMAGE surface (architect
#: Ruling 7). The image stage classifies on these via GeminiTransientError.
_TRANSIENT_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

#: ``generationConfig.imageConfig.aspectRatio`` for image generation. Fixed at
#: 16:9 until Slice 9 introduces the per-call ``aspect_ratio`` parameter.
_ASPECT_RATIO: Final[str] = "16:9"

#: ``generationConfig.imageConfig.imageSize`` — 2K yields ~2752x1536 images.
_IMAGE_SIZE: Final[str] = "2K"


class GeminiClient:
    """Lazy Gemini AI Studio client. Construct inside `stage.run()` only."""

    def __init__(self, api_key: SecretStr) -> None:
        if not api_key.get_secret_value():
            raise MissingApiKey("GEMINI_API_KEY")
        self._api_key = api_key

    def __repr__(self) -> str:
        return "<GeminiClient>"

    # ------------------------------------------------------------------ image
    def generate_image(
        self,
        prompt: str,
        *,
        model: str,
        seed: int,
        reference_image_bytes: bytes | None = None,
    ) -> bytes:
        """POST one prompt to AI Studio and return the generated image bytes.

        ONE HTTP call per invocation. Status classification:

        - 200 with an inline_data image → return the decoded bytes.
        - 200 with promptFeedback.blockReason (safety block) → raise
          :class:`GeminiNonTransientError`.
        - 200 without any inline_data part → raise
          :class:`GeminiNonTransientError` (the model returned text only).
        - 429 / 500 / 502 / 503 / 504 → raise :class:`GeminiTransientError`.
        - Any other non-2xx → raise :class:`GeminiNonTransientError`.

        Network timeouts/connection errors propagate raw so the owning stage's
        retry loop can classify them as transient.
        """
        import base64
        import json

        import requests

        url = f"{_API_BASE}/models/{model}:generateContent"
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if reference_image_bytes is not None:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": base64.b64encode(reference_image_bytes).decode("ascii"),
                    }
                }
            )
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "seed": seed,
                "responseModalities": ["IMAGE"],
                "imageConfig": {
                    "aspectRatio": _ASPECT_RATIO,
                    "imageSize": _IMAGE_SIZE,
                },
            },
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key.get_secret_value(),
        }
        response = requests.post(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            timeout=_HTTP_TIMEOUT_SEC,
        )
        return self._handle_image_response(response)

    @staticmethod
    def _handle_image_response(response: Any) -> bytes:
        import base64

        if response.status_code != 200:
            if response.status_code in _TRANSIENT_STATUS:
                raise GeminiTransientError(response.status_code, response.text)
            raise GeminiNonTransientError(response.status_code, response.text)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GeminiNonTransientError(
                response.status_code, f"non-JSON response body: {exc}"
            ) from exc

        feedback = payload.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise GeminiNonTransientError(
                response.status_code,
                f"content policy block: {feedback['blockReason']}",
            )

        for candidate in payload.get("candidates", []):
            for part in (candidate.get("content") or {}).get("parts", []):
                inline = part.get("inline_data") or part.get("inlineData")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])

        raise GeminiNonTransientError(
            response.status_code,
            "response had no inline_data part — model returned text only",
        )

    # ------------------------------------------------------------- multimodal
    def multimodal(
        self,
        prompt: str,
        images: list[Path],
        *,
        model: str = "gemini-2.5-pro",
    ) -> str:
        """Generate a text narrative from a prompt plus zero-or-more images.

        Stage 02 (`s02_enrich`) calls this with the changelog entry + diff
        stats folded into ``prompt`` and any feature-walkthrough screenshots in
        ``images``. ONE HTTP POST per call to the Gemini 2.5 Pro
        ``generateContent`` surface. ``images`` may be empty (the ``live_url``
        was omitted, so no screenshots were captured) — the call then sends a
        text-only request.

        Args:
            prompt: the rendered text prompt (entry + diff stats + framing).
            images: project-relative-or-absolute paths to ``.png`` screenshots
                to attach as inline image parts. May be empty.
            model: Gemini model id; defaults to the 2.5 Pro multimodal surface.

        Returns:
            The model's narrative text (concatenation of all returned text
            parts), stripped of leading/trailing whitespace.

        Raises:
            GeminiRateLimited: the surface returned HTTP 429 (terminal — no
                stage retry loop wraps this call).
            GeminiNonTransientError: any other non-200 status, a safety block,
                or a 200 carrying no text part.
            requests.Timeout / requests.ConnectionError: propagated raw.
        """
        import base64
        import json

        import requests

        url = f"{_API_BASE}/models/{model}:generateContent"
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for image_path in images:
            data = image_path.read_bytes()
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                }
            )
        body = {"contents": [{"parts": parts}]}
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key.get_secret_value(),
        }
        response = requests.post(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            timeout=_HTTP_TIMEOUT_SEC,
        )
        return self._handle_multimodal_response(response)

    @staticmethod
    def _handle_multimodal_response(response: Any) -> str:
        if response.status_code == 429:
            raise GeminiRateLimited(response.text)
        if response.status_code != 200:
            raise GeminiNonTransientError(response.status_code, response.text)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GeminiNonTransientError(
                response.status_code, f"non-JSON response body: {exc}"
            ) from exc

        feedback = payload.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise GeminiNonTransientError(
                response.status_code,
                f"content policy block: {feedback['blockReason']}",
            )

        texts: list[str] = []
        for candidate in payload.get("candidates", []):
            for part in (candidate.get("content") or {}).get("parts", []):
                text = part.get("text")
                if text:
                    texts.append(text)
        if not texts:
            raise GeminiNonTransientError(
                response.status_code,
                "multimodal response had no text part",
            )
        return "".join(texts).strip()
