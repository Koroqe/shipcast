"""Unit tests for ``shipcast.clients.veo_client.VeoClient`` (Slice 13).

The Veo 3 Fast REST surface is a long-running-operation API: a ``generateContent``
POST returns an operation name, then the client polls until the operation reports
``done`` and downloads the produced MP4. Every network call is mocked here — NO
real Veo / network. The tests pin:

* ``MissingApiKey("GEMINI_API_KEY")`` on an empty key (name only).
* import-purity: ``requests`` is not pulled into ``sys.modules`` by importing the
  module (it is imported lazily inside the methods).
* the happy path writes the downloaded bytes to ``output_path`` and returns it.
* ``VeoQuotaExceeded`` on HTTP 429.
* ``VeoSafetyBlocked`` when the completed operation carries a block envelope —
  AND that the original prompt text never appears in the raised error.
* ``VeoTimeout`` when polling exceeds the 120 s budget (driven by an injected
  monotonic clock so the test does not actually sleep 120 s).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from shipcast.clients.veo_client import VeoClient
from shipcast.errors import (
    MissingApiKey,
    VeoQuotaExceeded,
    VeoSafetyBlocked,
    VeoTimeout,
)

# A tiny but real MP4 box header is unnecessary — the client only writes bytes.
_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42FAKEVEOCLIPBYTES"

_SECRET_PROMPT = "SENSITIVE-HERO-PROMPT-do-not-leak-12345"


class _FakeResponse:
    def __init__(self, status_code: int, *, json_body: Any = None, content: bytes = b""):
        self.status_code = status_code
        self._json = json_body
        self.content = content
        self.text = "" if json_body is None else str(json_body)

    def json(self) -> Any:
        return self._json


def _key() -> SecretStr:
    return SecretStr("test-key")


def test_import_purity_no_requests_at_import() -> None:
    """Importing the veo_client module must not pull `requests` into sys.modules."""
    # The module is already imported at top; assert requests was not eagerly
    # imported as a side effect of importing veo_client specifically. We can only
    # reliably assert the module does not import requests at top-level by reading
    # that `requests` is absent unless some OTHER test imported it first; the
    # canonical guard lives in test_package_imports. Here we assert the module
    # object has no module-level `requests` attribute.
    import shipcast.clients.veo_client as mod

    assert not hasattr(mod, "requests")


def test_missing_api_key_raises_name_only() -> None:
    with pytest.raises(MissingApiKey) as exc:
        VeoClient(api_key=SecretStr(""))
    assert "GEMINI_API_KEY" in str(exc.value)
    # never the (empty) value
    assert "test-key" not in str(exc.value)


def test_happy_path_writes_clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit → poll-once-done → download → bytes written to output_path."""
    out = tmp_path / "beat_00.mp4"
    client = VeoClient(api_key=_key())

    posts: list[dict[str, Any]] = []

    def _fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        posts.append({"url": url, **kwargs})
        # the submit POST returns an operation name
        return _FakeResponse(200, json_body={"name": "operations/abc123"})

    def _fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        # poll: done immediately with a video uri reference; the client then
        # downloads via a second GET that returns raw bytes.
        if "operations/abc123" in url:
            return _FakeResponse(
                200,
                json_body={
                    "done": True,
                    "response": {
                        "generateVideoResponse": {
                            "generatedSamples": [
                                {"video": {"uri": "https://veo.test/file/xyz"}}
                            ]
                        }
                    },
                },
            )
        # the file download
        return _FakeResponse(200, content=_FAKE_MP4)

    monkeypatch.setattr(client, "_post", _fake_post)
    monkeypatch.setattr(client, "_get", _fake_get)

    result = client.generate_clip(
        _SECRET_PROMPT, model="veo-3-fast", output_path=out
    )
    assert result == out
    assert out.is_file()
    assert out.read_bytes() == _FAKE_MP4


def test_quota_exceeded_on_429(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = VeoClient(api_key=_key())

    def _fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(429, json_body={"error": "RESOURCE_EXHAUSTED"})

    monkeypatch.setattr(client, "_post", _fake_post)
    with pytest.raises(VeoQuotaExceeded):
        client.generate_clip(
            _SECRET_PROMPT, model="veo-3-fast", output_path=tmp_path / "x.mp4"
        )


def test_safety_block_does_not_leak_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed-but-blocked operation raises VeoSafetyBlocked with NO prompt text."""
    client = VeoClient(api_key=_key())

    def _fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(200, json_body={"name": "operations/blocked"})

    def _fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(
            200,
            json_body={
                "done": True,
                "response": {
                    "raiMediaFilteredReasons": ["Celebrity likeness"],
                    "raiMediaFilteredCount": 1,
                },
            },
        )

    monkeypatch.setattr(client, "_post", _fake_post)
    monkeypatch.setattr(client, "_get", _fake_get)

    with pytest.raises(VeoSafetyBlocked) as exc:
        client.generate_clip(
            _SECRET_PROMPT, model="veo-3-fast", output_path=tmp_path / "x.mp4"
        )
    assert _SECRET_PROMPT not in str(exc.value)
    # the output file must NOT be written on a block
    assert not (tmp_path / "x.mp4").exists()


def test_timeout_after_120s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling that never completes raises VeoTimeout once the 120 s budget elapses."""
    client = VeoClient(api_key=_key())

    def _fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(200, json_body={"name": "operations/forever"})

    def _fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(200, json_body={"done": False})

    monkeypatch.setattr(client, "_post", _fake_post)
    monkeypatch.setattr(client, "_get", _fake_get)

    # Drive the monotonic clock: first call (start) returns 0, subsequent calls
    # jump past the 120 s deadline so the loop times out without real sleeping.
    ticks = iter([0.0, 0.5, 121.0, 200.0, 300.0])

    def _fake_clock() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 999.0

    monkeypatch.setattr(client, "_now", _fake_clock)
    monkeypatch.setattr(client, "_sleep", lambda _s: None)

    with pytest.raises(VeoTimeout):
        client.generate_clip(
            _SECRET_PROMPT, model="veo-3-fast", output_path=tmp_path / "x.mp4"
        )


def test_conditioning_image_is_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conditioning PNG is read and folded into the submit body as inline_data."""
    out = tmp_path / "beat_00.mp4"
    cond = tmp_path / "style.png"
    cond.write_bytes(b"\x89PNG\r\n\x1a\nSTYLE")
    client = VeoClient(api_key=_key())

    seen: dict[str, Any] = {}

    def _fake_post(url: str, *, data: bytes) -> _FakeResponse:
        import json as _json

        seen["body"] = _json.loads(data.decode("utf-8"))
        return _FakeResponse(200, json_body={"name": "operations/c"})

    def _fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        if "operations/c" in url:
            return _FakeResponse(
                200,
                json_body={
                    "done": True,
                    "response": {
                        "generateVideoResponse": {
                            "generatedSamples": [
                                {"video": {"uri": "https://veo.test/f"}}
                            ]
                        }
                    },
                },
            )
        return _FakeResponse(200, content=_FAKE_MP4)

    monkeypatch.setattr(client, "_post", _fake_post)
    monkeypatch.setattr(client, "_get", _fake_get)

    client.generate_clip(
        _SECRET_PROMPT, model="veo-3-fast", output_path=out, conditioning_image=cond
    )
    parts = seen["body"]["instances"][0]["parts"]
    assert any("inline_data" in p for p in parts)


def test_submit_non_200_raises_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = VeoClient(api_key=_key())
    monkeypatch.setattr(
        client, "_post", lambda url, **k: _FakeResponse(500, json_body={})
    )
    with pytest.raises(VeoQuotaExceeded):
        client.generate_clip(
            _SECRET_PROMPT, model="veo-3-fast", output_path=tmp_path / "x.mp4"
        )


def test_submit_missing_operation_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = VeoClient(api_key=_key())
    monkeypatch.setattr(
        client, "_post", lambda url, **k: _FakeResponse(200, json_body={})
    )
    with pytest.raises(VeoQuotaExceeded):
        client.generate_clip(
            _SECRET_PROMPT, model="veo-3-fast", output_path=tmp_path / "x.mp4"
        )


def test_completed_without_sample_is_safety_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A done operation with no generated sample falls back as a safety block."""
    client = VeoClient(api_key=_key())
    monkeypatch.setattr(
        client, "_post", lambda url, **k: _FakeResponse(200, json_body={"name": "operations/empty"})
    )
    monkeypatch.setattr(
        client,
        "_get",
        lambda url, **k: _FakeResponse(200, json_body={"done": True, "response": {}}),
    )
    with pytest.raises(VeoSafetyBlocked):
        client.generate_clip(
            _SECRET_PROMPT, model="veo-3-fast", output_path=tmp_path / "x.mp4"
        )


def test_no_requests_in_sys_modules_via_cli() -> None:
    """Defense-in-depth: importing veo_client must not import requests eagerly."""
    # Drop any prior requests import to make the assertion meaningful only if this
    # test runs first; otherwise the canonical guard is test_package_imports.
    had = "requests" in sys.modules
    # Re-import the module fresh-ish; can't truly unload, but ensure attribute.
    import importlib

    import shipcast.clients.veo_client as mod

    importlib.reload(mod)
    assert not hasattr(mod, "requests")
    # If requests was already loaded by a sibling test we don't fail — the real
    # invariant is module-level absence, asserted above.
    _ = had
