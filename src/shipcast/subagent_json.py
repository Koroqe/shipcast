"""Tolerant JSON extraction for ``claude -p`` sub-agent stdout.

``claude -p`` (and the tailored agents) frequently wrap a JSON answer in a
short prose preamble and/or a Markdown ```json code fence — e.g.::

    I'll write the copy as a JSON object:

    ```json
    { ... }
    ```

A bare ``json.loads(stdout)`` then fails at column 1. The sub-agent stages
(``s02_enrich`` framing, ``s04_plan`` planner, ``s05_script``, ``s10_copy``)
call :func:`extract_json_object` to recover the embedded object before
parsing. Truly non-JSON output still fails downstream (the helper returns the
stripped text unchanged when no object is present), preserving the
``SubagentMalformedOutput`` error path.

LEAF MODULE — stdlib only; imported by stages, never the reverse.
"""

from __future__ import annotations

_FENCE = "```"


def extract_json_object(text: str) -> str:
    """Return the best-effort JSON-object substring of ``text``.

    Strips a Markdown code fence (``` or ```json) when present, then narrows to
    the outermost ``{ ... }`` span. If no ``{`` / ``}`` pair is found, the
    stripped input is returned unchanged so the caller's ``json.loads`` still
    raises on genuinely malformed output.
    """
    stripped = text.strip()

    # Unwrap a fenced block: take the content of the first ```...``` fence.
    if _FENCE in stripped:
        after = stripped.split(_FENCE, 1)[1]
        # Drop an optional language tag on the opening fence line (e.g. ```json).
        if "\n" in after:
            first_line, rest = after.split("\n", 1)
            if first_line.strip().isalpha():
                after = rest
        inner = after.split(_FENCE, 1)[0]
        stripped = inner.strip()

    # Narrow to the outermost object span.
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    return stripped
