"""Stage 05 — script (showcase storyboard).

Turns the marketing brief + enriched context into a ``Storyboard`` via a SINGLE
``claude -p`` sub-agent (``demo-script-writer``). The agent fleshes out the
planner's 4-beat skeleton into the showcase storyboard: 4-6 beats, each pairing
an ``image_prompt`` with a ``narration`` line and an on-screen ``duration_sec``
of 3-5 s.

Sub-agent error handling (mirrors ``s04_plan``)
-----------------------------------------------
The ``demo-script-writer`` call has a 300 s wall-clock budget. On timeout we
raise :class:`SubagentTimeout`, on a non-zero exit :class:`SubagentFailed`, and
on non-JSON / non-object stdout :class:`SubagentMalformedOutput` — each surfaces
through the dispatcher's FAILED transition (TC-8.5).

Beat-count rule (TC-8.3 / TC-8.4)
---------------------------------
``run`` enforces the 4-6-beat bound against the parsed sub-agent JSON BEFORE
writing anything, raising :class:`SubagentMalformedOutput` for a count outside
the range. The ``Storyboard`` schema duplicates the bound so the default
``validate_outputs`` re-checks it on disk as defense-in-depth, and a beat that
is otherwise malformed (e.g. a missing ``narration`` field, TC-8.6) fails the
``Storyboard.model_validate`` call in ``run`` (and again in ``validate_outputs``)
before any partial ``storyboard.json`` is left on disk.

Cost
----
The sub-agent authenticates via the operator's local ``claude`` subscription and
incurs NO per-call USD cost, so this stage keeps the BaseStage default
``next_call_cost_usd`` of 0.0 and is never cost-gated.

Determinism (TC-21.3)
---------------------
The artifact JSON wrapper contains no ``datetime.now()`` and no random id;
non-determinism is confined to the single sub-agent ``run()`` call.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from shipcast.errors import (
    SubagentFailed,
    SubagentMalformedOutput,
    SubagentTimeout,
)
from shipcast.manifest import StageStatus, dump_json_canonical
from shipcast.schemas import Storyboard
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage
from shipcast.subagent_json import extract_json_object

if TYPE_CHECKING:
    from collections.abc import Callable

    from shipcast.project import Project

#: Wall-clock budget for the demo-script-writer sub-agent invocation.
_SUBAGENT_TIMEOUT_SEC: int = 600

#: Inclusive beat-count bounds for a showcase storyboard.
_MIN_BEATS: int = 4
_MAX_BEATS: int = 6


class ScriptStage(BaseStage):
    """Produce ``05_script/storyboard.json`` via the demo-script-writer agent."""

    id: ClassVar[str] = "05_script"
    requires: ClassVar[tuple[str, ...]] = ("04_plan",)
    output_schema: ClassVar[type[Storyboard]] = Storyboard
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Read each beat's narration aloud — confirm it is on-brand and reads in "
        "the beat's duration.",
        "Confirm each image_prompt describes a concrete, on-brand shot.",
        "Verify the storyboard has 4-6 beats and every duration is 3-5 s.",
        "Check the opening beat lands the hook and the closing beat points at a CTA.",
    )

    OUTPUT_FILENAME: ClassVar[str] = "storyboard.json"
    BRIEF_REL: ClassVar[str] = "04_plan/brief.json"
    ENTRY_REL: ClassVar[str] = "01_pick/entry.json"
    CONTEXT_REL: ClassVar[str] = "02_enrich/context.json"

    def __init__(
        self,
        *,
        subprocess_run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        # Indirection so tests can inject a fake `claude -p` without patching the
        # global `subprocess` module. Defaults to the real `subprocess.run`.
        self._subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = (
            subprocess_run or subprocess.run
        )

    # ------------------------------------------------------------- context read
    def _read_text(self, project: Project, rel: str) -> str:
        """Read an upstream artifact as text, tolerating absence (→ "")."""
        path = project.path / rel
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------- sub-agent
    def _invoke_subagent(self, agent: str, prompt: str) -> dict[str, object]:
        """Run one ``claude -p`` call and parse its JSON stdout.

        Uses a plain (default-agent) ``claude -p``: the tailored
        ``demo-script-writer`` agent has Write/Edit tools and tends to WRITE a
        file under ``claude -p`` instead of printing JSON to stdout (empty
        stdout → malformed-output). The prompt is fully self-contained, so a
        plain call returning JSON on stdout is the reliable shape. ``agent`` is
        retained only as the error label.

        Raises:
            SubagentTimeout: the subprocess exceeded the 300 s budget.
            SubagentFailed: the subprocess exited non-zero (stderr captured).
            SubagentMalformedOutput: stdout was not a JSON object.
        """
        try:
            result = self._subprocess_run(
                [
                    "claude",
                    "-p",
                    "--output-format",
                    "text",
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=_SUBAGENT_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as exc:
            raise SubagentTimeout(
                f"{agent} exceeded {_SUBAGENT_TIMEOUT_SEC}s timeout"
            ) from exc

        if result.returncode != 0:
            raise SubagentFailed(agent, result.returncode, result.stderr or "")

        try:
            parsed = json.loads(extract_json_object(result.stdout))
        except json.JSONDecodeError as exc:
            raise SubagentMalformedOutput(
                f"{agent} stdout was not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise SubagentMalformedOutput(
                f"{agent} JSON must be an object, got {type(parsed).__name__}"
            )
        return parsed

    # ------------------------------------------------------------- prompt
    @staticmethod
    def _build_prompt(brief_json: str, entry_json: str, context_json: str) -> str:
        """Assemble the deterministic demo-script-writer prompt."""
        return (
            "Draft the showcase storyboard for this changelog entry as a single "
            "JSON object matching the Storyboard schema. Print ONLY the JSON "
            "object to stdout (no markdown, no prose, no code fence). Do NOT "
            "use the Write/Edit tools, create files, or read files — answer "
            "purely from the context below.\n\n"
            "The object MUST have exactly one key, `beats`: an array of 4-6 "
            "objects, each with image_prompt (str), narration (str), and "
            "duration_sec (a number between 3 and 5 inclusive). Build on the "
            "brief's video_beats skeleton; open on the hook and close on a CTA.\n\n"
            f"Marketing brief (04_plan/brief.json):\n{brief_json}\n\n"
            f"Picked entry (01_pick/entry.json):\n{entry_json}\n\n"
            f"Enriched context (02_enrich/context.json):\n{context_json}\n"
        )

    # ------------------------------------------------------------- run
    def run(self, project: Project) -> StageResult:
        """Invoke demo-script-writer, validate, write ``05_script/storyboard.json``.

        The 4-6-beat bound is enforced against the parsed JSON BEFORE the
        ``Storyboard`` validation so an out-of-range count surfaces as
        ``SubagentMalformedOutput`` (TC-8.3 / TC-8.4). The full schema validation
        (durations, required fields) runs next and raises before any file is
        written, so a malformed beat leaves no partial ``storyboard.json``
        (TC-8.6).
        """
        brief_json = self._read_text(project, self.BRIEF_REL)
        entry_json = self._read_text(project, self.ENTRY_REL)
        context_json = self._read_text(project, self.CONTEXT_REL)

        parsed = self._invoke_subagent(
            "demo-script-writer",
            self._build_prompt(brief_json, entry_json, context_json),
        )

        # Beat-count check FIRST → SubagentMalformedOutput for an out-of-range
        # count (TC-8.3 / TC-8.4), distinct from a per-beat schema failure.
        beats = parsed.get("beats")
        if not isinstance(beats, list) or not (
            _MIN_BEATS <= len(beats) <= _MAX_BEATS
        ):
            count = len(beats) if isinstance(beats, list) else "missing"
            raise SubagentMalformedOutput(
                f"demo-script-writer must return {_MIN_BEATS}-{_MAX_BEATS} beats, "
                f"got {count}"
            )

        # Full schema validation (durations 3-5 s, required beat fields). A
        # failure here raises before any file is written.
        storyboard = Storyboard.model_validate(parsed)

        output_path = project.artifact_path(self.id, self.OUTPUT_FILENAME)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            dump_json_canonical(storyboard.model_dump(mode="json")),
            encoding="utf-8",
        )

        return StageResult(
            status=StageStatus.DONE,
            outputs=(Path(self.id) / self.OUTPUT_FILENAME,),
            metrics={
                "cost_usd": 0.0,
                "beats": len(storyboard.beats),
            },
        )
