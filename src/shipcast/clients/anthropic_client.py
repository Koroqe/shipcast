"""Claude client — invokes the operator's local `claude` CLI subprocess.

This implementation uses the operator's existing Claude subscription via the
`claude` CLI tool (installed and authenticated separately). It does NOT use
the Anthropic API and therefore does NOT need an API key — auth is whatever
`claude` is signed in to on the operator's machine.

The class name `AnthropicClient` is retained for architectural continuity
(stages call `from shipcast.clients import AnthropicClient`), but the body is
a thin wrapper over `subprocess.run(["claude", "-p", ...])`. The cost model
becomes "free under your Claude subscription" instead of "pay-per-token".

Failure modes:
- `claude` not on PATH → `FileNotFoundError` propagates.
- `claude` exits non-zero (auth failure, model error, etc.) → `CalledProcessError`
  propagates.
- Timeout (default 600s) → `TimeoutExpired` propagates.

All three surface in the dispatcher's FAILED transition with the structured
exception type recorded in the manifest.
"""

from __future__ import annotations

import subprocess


class AnthropicClient:
    """Thin wrapper over the `claude` CLI subprocess.

    No constructor arguments — auth is handled by the `claude` CLI itself.
    Instances are stateless; they exist to preserve the
    `client = AnthropicClient(); client.generate_text(...)` call shape that
    stages 02 / 06 / 07 / 08 use.
    """

    #: Maximum seconds to wait for a single Claude invocation.
    DEFAULT_TIMEOUT_SEC: int = 600

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    def generate_text(
        self, prompt: str, model: str, max_tokens: int = 2048
    ) -> str:
        """Send `prompt` to `claude -p` and return the response text.

        `max_tokens` is accepted for signature compatibility with the
        original Anthropic SDK shape but is NOT enforced — the `claude` CLI
        does not expose a max-tokens flag in print mode and uses its own
        defaults. Stage code may still record the requested value in metrics
        for diagnostic purposes.

        Args:
            prompt: the full text prompt (rendered template) sent to Claude.
            model: model identifier accepted by `claude --model` (e.g.,
                `"claude-opus-4-7"`, `"opus"`, `"sonnet"`).
            max_tokens: requested response budget; not currently enforced by
                the CLI, retained for future compatibility.

        Returns:
            The model's response text exactly as `claude -p` printed to stdout
            (with no extra processing; trailing newline preserved if present).

        Raises:
            FileNotFoundError: `claude` is not on PATH.
            subprocess.CalledProcessError: `claude` exited non-zero (auth
                failure, model not found, content policy, etc.).
            subprocess.TimeoutExpired: invocation exceeded DEFAULT_TIMEOUT_SEC.
        """
        _ = max_tokens  # accepted but not enforced; documented above
        result = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "text", prompt],
            capture_output=True,
            text=True,
            check=True,
            timeout=self.DEFAULT_TIMEOUT_SEC,
        )
        return result.stdout
