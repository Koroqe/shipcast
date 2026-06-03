"""Unit tests for `GeminiClient.generate_image` aspect-ratio support (Slice 9).

Slice 9 adds an ``aspect_ratio`` parameter to ``generate_image`` mapping each
of the five supported ratios to canonical pixel dimensions and threading those
dimensions through the Gemini Imagen request payload:

    1:1   → 1024x1024
    16:9  → 1920x1080   (default — existing callers unaffected)
    9:16  → 1080x1920
    4:5   → 1080x1350
    og    → 1200x630    (the OG card)

The default stays ``16:9`` so any existing 16:9 caller (none in production yet,
but Slice 13/16 will call this) keeps the prior behaviour — a dedicated
regression test asserts this.

`requests` is imported lazily INSIDE the client method, so we patch
`requests.post` on the real `requests` module (the lazy import resolves to the
same module object). No real HTTP call is ever made.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any, get_args
from unittest.mock import MagicMock

import pytest
import requests
from PIL import Image
from pydantic import SecretStr

from shipcast.clients.gemini_client import (
    ASPECT_RATIO_DIMENSIONS,
    AspectRatio,
    GeminiClient,
)
from shipcast.errors import GeminiNonTransientError, GeminiTransientError

SENTINEL_KEY = "AIzaSyTestSentinelDoNotLog123456789"


def _png_bytes(width: int, height: int) -> bytes:
    """A real, decodable PNG of the requested size (so PIL can read it back)."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buf, format="PNG")
    return buf.getvalue()


def _image_response(image_bytes: bytes, *, status: int = 200) -> MagicMock:
    """A mocked Gemini 200 response carrying one inline_data image part."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    payload: dict[str, Any] = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }
    resp.text = json.dumps({"ok": True})
    resp.json.return_value = payload
    return resp


def _captured_image_config(post: MagicMock) -> dict[str, Any]:
    body = json.loads(post.call_args.kwargs["data"])
    image_config: dict[str, Any] = body["generationConfig"]["imageConfig"]
    return image_config


# --------------------------------------------------------------------------- #
# Mapping table
# --------------------------------------------------------------------------- #


def test_mapping_table_covers_every_literal() -> None:
    """ASPECT_RATIO_DIMENSIONS has exactly one entry per AspectRatio literal."""
    literals = set(get_args(AspectRatio))
    assert set(ASPECT_RATIO_DIMENSIONS) == literals


def test_mapping_table_canonical_dimensions() -> None:
    assert ASPECT_RATIO_DIMENSIONS == {
        "1:1": (1024, 1024),
        "16:9": (1920, 1080),
        "9:16": (1080, 1920),
        "4:5": (1080, 1350),
        "og": (1200, 630),
    }


# --------------------------------------------------------------------------- #
# Each aspect ratio yields a correctly-sized image and sends correct dims
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("aspect_ratio", "expected"),
    [
        ("1:1", (1024, 1024)),
        ("16:9", (1920, 1080)),
        ("9:16", (1080, 1920)),
        ("4:5", (1080, 1350)),
        ("og", (1200, 630)),
    ],
)
def test_generate_image_dimensions_per_ratio(
    monkeypatch: pytest.MonkeyPatch,
    aspect_ratio: AspectRatio,
    expected: tuple[int, int],
) -> None:
    """Each ratio: returned bytes decode to the canonical size AND the request
    payload carries those same width/height dimensions."""
    width, height = expected
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    post = MagicMock(return_value=_image_response(_png_bytes(width, height)))
    monkeypatch.setattr(requests, "post", post)

    out = client.generate_image(
        "a hero card",
        model="gemini-3-pro-image-preview",
        seed=7,
        aspect_ratio=aspect_ratio,
    )

    # The returned bytes are a valid PNG of the expected size.
    img = Image.open(io.BytesIO(out))
    assert img.size == expected

    # The request payload carried the same dimensions through to Gemini.
    image_config = _captured_image_config(post)
    assert image_config["imageSize"] == {"width": width, "height": height}

    # API key never leaks into the request body.
    assert SENTINEL_KEY not in post.call_args.kwargs["data"].decode("utf-8")


# --------------------------------------------------------------------------- #
# Regression — default behaviour for existing 16:9 callers is unchanged
# --------------------------------------------------------------------------- #


def test_generate_image_default_is_16x9(monkeypatch: pytest.MonkeyPatch) -> None:
    """No aspect_ratio arg → 16:9 (1920x1080). Existing callers unaffected."""
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    post = MagicMock(return_value=_image_response(_png_bytes(1920, 1080)))
    monkeypatch.setattr(requests, "post", post)

    out = client.generate_image(
        "a fill shot",
        model="gemini-3-pro-image-preview",
        seed=1,
    )

    img = Image.open(io.BytesIO(out))
    assert img.size == (1920, 1080)

    image_config = _captured_image_config(post)
    assert image_config["imageSize"] == {"width": 1920, "height": 1080}


def test_generate_image_explicit_16x9_matches_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing aspect_ratio="16:9" explicitly produces the same payload as the
    default — proving the default is genuinely 16:9 and nothing else changed."""
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))

    post_default = MagicMock(return_value=_image_response(_png_bytes(1920, 1080)))
    monkeypatch.setattr(requests, "post", post_default)
    client.generate_image("p", model="m", seed=3)
    default_body = json.loads(post_default.call_args.kwargs["data"])

    post_explicit = MagicMock(return_value=_image_response(_png_bytes(1920, 1080)))
    monkeypatch.setattr(requests, "post", post_explicit)
    client.generate_image("p", model="m", seed=3, aspect_ratio="16:9")
    explicit_body = json.loads(post_explicit.call_args.kwargs["data"])

    assert default_body == explicit_body


# --------------------------------------------------------------------------- #
# reference_image_bytes still threads through alongside aspect_ratio
# --------------------------------------------------------------------------- #


def test_generate_image_reference_bytes_with_aspect_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reference image is still attached as an inline_data part, and the
    aspect_ratio dimensions are honoured at the same time."""
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    post = MagicMock(return_value=_image_response(_png_bytes(1080, 1920)))
    monkeypatch.setattr(requests, "post", post)

    client.generate_image(
        "conditioned shot",
        model="m",
        seed=9,
        reference_image_bytes=_png_bytes(8, 8),
        aspect_ratio="9:16",
    )

    body = json.loads(post.call_args.kwargs["data"])
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "conditioned shot"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert body["generationConfig"]["imageConfig"]["imageSize"] == {
        "width": 1080,
        "height": 1920,
    }


# --------------------------------------------------------------------------- #
# Error classification is unchanged by the new parameter
# --------------------------------------------------------------------------- #


def test_generate_image_429_still_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 429
    resp.text = "rate limited"
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    monkeypatch.setattr(requests, "post", MagicMock(return_value=resp))
    with pytest.raises(GeminiTransientError):
        client.generate_image("p", model="m", seed=1, aspect_ratio="og")


def test_generate_image_safety_block_still_non_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    payload = {"promptFeedback": {"blockReason": "SAFETY"}}
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    monkeypatch.setattr(requests, "post", MagicMock(return_value=resp))
    with pytest.raises(GeminiNonTransientError):
        client.generate_image("p", model="m", seed=1, aspect_ratio="4:5")
