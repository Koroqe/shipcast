"""Unit tests for `GeminiClient.multimodal` (Slice 7).

The `multimodal` surface generates the `s02_enrich` narrative from a text
prompt plus zero-or-more inline image parts. There is NO stage-owned retry
loop for this call, so an HTTP 429 surfaces as `GeminiRateLimited` (TC-5.7).

`requests` is imported lazily INSIDE the client method, so we patch
`requests.post` on the real `requests` module — the lazy import resolves to
the same module object. No real HTTP call is ever made.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from pydantic import SecretStr

from shipcast.clients.gemini_client import GeminiClient
from shipcast.errors import (
    GeminiNonTransientError,
    GeminiRateLimited,
    MissingApiKey,
)

SENTINEL_KEY = "AIzaSyTestSentinelDoNotLog123456789"
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _text_response(text: str, *, status: int = 200) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    payload: dict[str, Any] = {
        "candidates": [{"content": {"parts": [{"text": text}]}}]
    }
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    return resp


def _status_response(status: int, body: str = "boom") -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.text = body
    resp.json.return_value = {}
    return resp


# --------------------------------------------------------------------------- #
# Construction / auth
# --------------------------------------------------------------------------- #


def test_empty_key_raises_missing_api_key() -> None:
    with pytest.raises(MissingApiKey) as exc:
        GeminiClient(api_key=SecretStr(""))
    assert str(exc.value) == "GEMINI_API_KEY"


# --------------------------------------------------------------------------- #
# Happy path — text-only and with images
# --------------------------------------------------------------------------- #


def test_multimodal_text_only_returns_narrative(monkeypatch: pytest.MonkeyPatch) -> None:
    """No images → single text part request, returns stripped narrative."""
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    post = MagicMock(return_value=_text_response("  a great narrative  "))
    monkeypatch.setattr(requests, "post", post)

    out = client.multimodal("describe this change", [])
    assert out == "a great narrative"
    # One POST; body carries exactly one text part, no inline_data.
    body = json.loads(post.call_args.kwargs["data"])
    parts = body["contents"][0]["parts"]
    assert parts == [{"text": "describe this change"}]
    # API key never leaks into the body.
    assert SENTINEL_KEY not in json.dumps(body)


def test_multimodal_with_images_attaches_inline_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Image paths become base64 inline_data parts."""
    img = tmp_path / "shot.png"
    img.write_bytes(TINY_PNG)
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    post = MagicMock(return_value=_text_response("narrative with screenshot"))
    monkeypatch.setattr(requests, "post", post)

    out = client.multimodal("prompt", [img])
    assert out == "narrative with screenshot"
    body = json.loads(post.call_args.kwargs["data"])
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "prompt"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert parts[1]["inline_data"]["data"]  # non-empty base64


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #


def test_multimodal_429_raises_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-5.7 unit half: HTTP 429 → GeminiRateLimited (terminal, no retry loop)."""
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    monkeypatch.setattr(requests, "post", MagicMock(return_value=_status_response(429)))
    with pytest.raises(GeminiRateLimited):
        client.multimodal("prompt", [])


def test_multimodal_500_raises_non_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-429 non-200 → GeminiNonTransientError (the narrative call has no retry)."""
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    monkeypatch.setattr(requests, "post", MagicMock(return_value=_status_response(500)))
    with pytest.raises(GeminiNonTransientError):
        client.multimodal("prompt", [])


def test_multimodal_safety_block_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    payload = {"promptFeedback": {"blockReason": "SAFETY"}}
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    monkeypatch.setattr(requests, "post", MagicMock(return_value=resp))
    with pytest.raises(GeminiNonTransientError):
        client.multimodal("prompt", [])


def test_multimodal_no_text_part_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    payload = {"candidates": [{"content": {"parts": [{"foo": "bar"}]}}]}
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    client = GeminiClient(api_key=SecretStr(SENTINEL_KEY))
    monkeypatch.setattr(requests, "post", MagicMock(return_value=resp))
    with pytest.raises(GeminiNonTransientError):
        client.multimodal("prompt", [])
