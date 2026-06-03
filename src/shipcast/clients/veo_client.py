"""Veo 3 Fast client — premium-mode hero-clip generation (Stage 06).

Constructed lazily inside ``s06_video_assets.run()`` only. The heavy
``requests`` import lives INSIDE the default ``_post`` / ``_get`` seams (not at
module top), so importing this module — or any transitive import from
``cli.py`` / ``shipcast.stages`` — does NOT pull ``requests`` into
``sys.modules``. This preserves the Slice-1 import-purity invariant. Auth is via
the operator's ``GEMINI_API_KEY`` SecretStr (Veo 3 Fast lives behind the same
AI-Studio key as Imagen), passed as the ``x-goog-api-key`` header.

Long-running-operation shape
----------------------------
Veo is asynchronous. ``generate_clip`` does:

1. POST the text prompt (+ optional conditioning image) to
   ``<model>:predictLongRunning`` → an operation ``name``.
2. Poll ``GET /v1beta/<operation-name>`` until ``done == true`` OR the 120 s
   wall-clock budget elapses (``VeoTimeout``).
3. On a completed-but-blocked operation → ``VeoSafetyBlocked`` (the premium
   stage falls back to Imagen + Ken-Burns for that beat).
4. Otherwise download the produced MP4 bytes and write them to ``output_path``.

An HTTP 429 on the submit POST → ``VeoQuotaExceeded`` (HARD failure).

SECURITY (Slice 13 pre-review)
------------------------------
On a safety block the original ``prompt`` is NEVER folded into the raised
``VeoSafetyBlocked`` (its constructor accepts only a non-sensitive
``block_reason`` token) and is never logged here — the client does no logging at
all. The stage's fallback path likewise avoids logging the blocked prompt.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import SecretStr

from shipcast.errors import (
    MissingApiKey,
    VeoQuotaExceeded,
    VeoSafetyBlocked,
    VeoTimeout,
)

if TYPE_CHECKING:
    from collections.abc import Callable

#: AI Studio v1beta surface — hosts the Veo long-running-operation endpoints.
_API_BASE: Final[str] = "https://generativelanguage.googleapis.com/v1beta"

#: Per-request HTTP timeout (seconds). The OVERALL job budget is the separate
#: 120 s poll deadline below.
_HTTP_TIMEOUT_SEC: Final[float] = 60.0

#: Wall-clock budget for the poll loop. Exceeding it raises ``VeoTimeout``.
_POLL_BUDGET_SEC: Final[float] = 120.0

#: Seconds between poll attempts.
_POLL_INTERVAL_SEC: Final[float] = 5.0


class VeoClient:
    """Lazy Veo 3 Fast client. Construct inside ``stage.run()`` only.

    Network is reached only through the ``_post`` / ``_get`` seams (which import
    ``requests`` lazily) and the clock through ``_now`` / ``_sleep`` — tests
    override these to drive the long-running-operation flow without real
    network or real sleeping.
    """

    def __init__(self, api_key: SecretStr) -> None:
        if not api_key.get_secret_value():
            raise MissingApiKey("GEMINI_API_KEY")
        self._api_key = api_key
        # Injectable seams (defaults wrap requests / time lazily).
        self._post: Callable[..., Any] = self._default_post
        self._get: Callable[..., Any] = self._default_get
        self._now: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep

    def __repr__(self) -> str:
        return "<VeoClient>"

    # ----------------------------------------------------------- default seams
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key.get_secret_value(),
        }

    def _default_post(self, url: str, *, data: bytes) -> Any:
        import requests

        return requests.post(
            url, data=data, headers=self._headers(), timeout=_HTTP_TIMEOUT_SEC
        )

    def _default_get(self, url: str) -> Any:
        import requests

        return requests.get(url, headers=self._headers(), timeout=_HTTP_TIMEOUT_SEC)

    # ----------------------------------------------------------- public surface
    def generate_clip(
        self,
        prompt: str,
        *,
        model: str,
        output_path: Path,
        conditioning_image: Path | None = None,
    ) -> Path:
        """Generate one 8-second Veo clip and write it to ``output_path``.

        Args:
            prompt: the hero-beat ``image_prompt`` text.
            model: the Veo model id (e.g. ``veo-3-fast``).
            output_path: absolute path the MP4 bytes are written to.
            conditioning_image: optional style-sheet PNG used to condition the
                generation (``03_brand/style_sheet.png``).

        Returns:
            ``output_path`` on success.

        Raises:
            VeoQuotaExceeded: submit POST returned HTTP 429.
            VeoSafetyBlocked: the completed operation carried a block envelope.
                The original ``prompt`` is NOT included in the error.
            VeoTimeout: polling exceeded the 120 s budget.
        """
        import base64
        import json

        parts: list[dict[str, Any]] = [{"text": prompt}]
        if conditioning_image is not None:
            data = conditioning_image.read_bytes()
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                }
            )
        body = {"instances": [{"prompt": prompt, "parts": parts}]}
        submit_url = f"{_API_BASE}/models/{model}:predictLongRunning"
        submit = self._post(submit_url, data=json.dumps(body).encode("utf-8"))
        if submit.status_code == 429:
            # No prompt in the error — quota is a content-agnostic condition.
            raise VeoQuotaExceeded(
                "Veo 3 Fast quota exhausted (HTTP 429); rerun after quota reset."
            )
        if submit.status_code != 200:
            raise VeoQuotaExceeded(
                f"Veo 3 Fast submit failed with HTTP {submit.status_code}."
            )
        operation_name = (submit.json() or {}).get("name")
        if not operation_name:
            raise VeoQuotaExceeded("Veo 3 Fast submit returned no operation name.")

        result = self._poll_until_done(operation_name)
        download_uri = self._extract_video_uri(result)
        clip_bytes = self._download(download_uri)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(clip_bytes)
        return output_path

    # ----------------------------------------------------------- poll + extract
    def _poll_until_done(self, operation_name: str) -> dict[str, Any]:
        """Poll the operation until ``done`` or the 120 s budget elapses."""
        op_url = f"{_API_BASE}/{operation_name}"
        deadline = self._now() + _POLL_BUDGET_SEC
        while True:
            resp = self._get(op_url)
            payload: dict[str, Any] = resp.json() or {}
            if payload.get("done"):
                return payload
            if self._now() >= deadline:
                raise VeoTimeout(
                    f"Veo 3 Fast job did not complete within "
                    f"{int(_POLL_BUDGET_SEC)} s."
                )
            self._sleep(_POLL_INTERVAL_SEC)

    @staticmethod
    def _extract_video_uri(operation: dict[str, Any]) -> str:
        """Pull the produced clip's download URI, or raise on a safety block."""
        response = operation.get("response") or {}
        # Safety / RAI filtering surfaces as a filtered-reasons envelope with no
        # generated sample. Surface it WITHOUT echoing the prompt.
        if response.get("raiMediaFilteredCount") or response.get(
            "raiMediaFilteredReasons"
        ):
            reasons = response.get("raiMediaFilteredReasons") or ["unspecified"]
            reason = reasons[0] if isinstance(reasons, list) and reasons else "unspecified"
            raise VeoSafetyBlocked(str(reason))
        samples = (
            (response.get("generateVideoResponse") or {}).get("generatedSamples")
            or []
        )
        for sample in samples:
            uri = ((sample or {}).get("video") or {}).get("uri")
            if uri:
                return str(uri)
        # No sample and no explicit filter envelope — treat as a block so the
        # premium stage can fall back rather than crash.
        raise VeoSafetyBlocked("no-sample")

    def _download(self, uri: str) -> bytes:
        resp = self._get(uri)
        content: bytes = resp.content
        return content
