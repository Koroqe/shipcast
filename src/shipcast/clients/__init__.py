"""Thin wrappers around external services.

Clients are instantiated LAZILY inside `stage.run()` only — never at module
import or CLI startup. API-key validation happens in each client's `__init__`,
so missing keys surface as `MissingApiKey("<KEY_NAME>")` at the moment a stage
needs them, not during unrelated stage tests.

The AI-client classes are exposed via lazy `__getattr__` so that
`from shipcast.clients import X` works for callers that ask for them, but
`import shipcast.clients` (or any transitive import from `cli.py`) does NOT
eagerly pull in the heavy SDKs. This preserves the lazy-construction
invariant (the SDKs and the `requests` library are not imported at CLI
startup; startup time unaffected). `check_available_or_raise` is eager
because it has no heavy import cost.

Slice 1 ships Anthropic / ElevenLabs / WhisperX + the ffmpeg availability
check. `GeminiClient` (Slice 9) and `PlaywrightClient` (Slice 8) are added to
the lazy registry by their owning slices.
"""

from __future__ import annotations

from typing import Any

from shipcast.clients.ffmpeg_client import check_available_or_raise

__all__ = [
    "AnthropicClient",
    "ElevenLabsClient",
    "WhisperXClient",
    "check_available_or_raise",
]


def __getattr__(name: str) -> Any:
    """Lazy import of AI client classes (keeps heavy SDKs out of CLI startup)."""
    if name == "AnthropicClient":
        from shipcast.clients.anthropic_client import AnthropicClient

        return AnthropicClient
    if name == "ElevenLabsClient":
        from shipcast.clients.elevenlabs_client import ElevenLabsClient

        return ElevenLabsClient
    if name == "WhisperXClient":
        from shipcast.clients.whisperx_client import WhisperXClient

        return WhisperXClient
    raise AttributeError(f"module 'shipcast.clients' has no attribute {name!r}")
