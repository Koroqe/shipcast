"""Stage 04 — plan (marketing brief).

Turns the brand contract + enriched context into a ``MarketingBrief`` via a
CHAINED, SEQUENTIAL pair of ``claude -p`` sub-agents:

1. ``planner`` — drafts the brief (hook-template per channel, CTAs, a 4-beat
   video skeleton, 4 carousel beats, stat/code flags).
2. ``brand-guardian`` — consumes the planner's draft JSON together with the
   brand pack (``03_brand/voice.md`` + ``03_brand/proposal.json``) and returns
   the FINAL, voice/visual-conformant version. The guardian's output is what we
   persist — it OVERRIDES the planner's draft (TC-7.3).

The two sub-agents run strictly one-after-the-other (NOT concurrently): the
guardian cannot run until it has the planner's draft to guard. Each call has a
300 s wall-clock budget; on timeout we raise :class:`SubagentTimeout`, on a
non-zero exit :class:`SubagentFailed`, and on non-JSON stdout
:class:`SubagentMalformedOutput` — each surfaces through the dispatcher's FAILED
transition (TC-7.4 / TC-7.5 / TC-7.6).

voice.md read-path (Architect MAJOR Finding 1)
----------------------------------------------
This stage reads the CANONICAL ``03_brand/voice.md`` (the copy ``s03_brand``
wrote as a declared, hash-covered output) — never the raw ``_brand/<slug>/``
pack. The default ``BaseStage.check_inputs`` already requires every declared
``03_brand`` output (including ``voice.md``) to exist on disk, so a deleted
``03_brand/voice.md`` raises ``StageInputMissing`` before ``run`` (TC-20.2).

Cost
----
Sub-agents authenticate via the operator's local ``claude`` subscription and
incur NO per-call USD cost, so ``next_call_cost_usd`` keeps the BaseStage
default of 0.0 — this stage is never cost-gated.

Determinism (TC-21.3)
---------------------
The artifact JSON wrapper contains no ``datetime.now()`` and no random id;
non-determinism is confined to the two sub-agent ``run()`` calls.
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
from shipcast.schemas import MarketingBrief
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage
from shipcast.subagent_json import extract_json_object

if TYPE_CHECKING:
    from collections.abc import Callable

    from shipcast.project import Project

#: Wall-clock budget for EACH sub-agent invocation (planner, brand-guardian).
_SUBAGENT_TIMEOUT_SEC: int = 300


class PlanStage(BaseStage):
    """Produce ``04_plan/brief.json`` via chained planner → brand-guardian."""

    id: ClassVar[str] = "04_plan"
    requires: ClassVar[tuple[str, ...]] = ("03_brand",)
    output_schema: ClassVar[type[MarketingBrief]] = MarketingBrief
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Confirm each channel's hook template fits the entry (one of the 7 catalog keys).",
        "Read the CTAs — confirm they match the voice.md CTA pattern and are on-brand.",
        "Skim the 4 video beats and 4 carousel beats for accuracy and brand voice.",
        "Verify has_stat_card / has_code_screenshot reflect what the entry actually warrants.",
    )

    OUTPUT_FILENAME: ClassVar[str] = "brief.json"
    #: Canonical voice contract written by s03_brand (Finding-1 read-path).
    VOICE_REL: ClassVar[str] = "03_brand/voice.md"
    PROPOSAL_REL: ClassVar[str] = "03_brand/proposal.json"
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

    # ------------------------------------------------------------- sub-agents
    def _invoke_subagent(
        self, agent: str | None, prompt: str, *, label: str | None = None
    ) -> dict[str, object]:
        """Run one ``claude -p`` call and parse its JSON stdout.

        When ``agent`` is a name, the call is ``claude -p --agent <agent>``
        (used for the tailored shipcast agents). When ``agent`` is ``None``,
        a plain ``claude -p`` (default agent) is used — the right-sized tool
        for a bounded, self-contained one-shot JSON call such as the planner
        draft, where the stock ``planner`` agent would over-work and time out.
        ``label`` names the call in errors when ``agent`` is ``None``.

        Raises:
            SubagentTimeout: the subprocess exceeded the 300 s budget.
            SubagentFailed: the subprocess exited non-zero (stderr captured).
            SubagentMalformedOutput: stdout was not a JSON object.
        """
        who = agent or label or "subagent"
        cmd = ["claude", "-p"]
        if agent is not None:
            cmd += ["--agent", agent]
        cmd += ["--output-format", "text", prompt]
        try:
            result = self._subprocess_run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_SUBAGENT_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as exc:
            raise SubagentTimeout(
                f"{who} exceeded {_SUBAGENT_TIMEOUT_SEC}s timeout"
            ) from exc

        if result.returncode != 0:
            raise SubagentFailed(who, result.returncode, result.stderr or "")

        try:
            parsed = json.loads(extract_json_object(result.stdout))
        except json.JSONDecodeError as exc:
            raise SubagentMalformedOutput(
                f"{who} stdout was not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise SubagentMalformedOutput(
                f"{who} JSON must be an object, got {type(parsed).__name__}"
            )
        return parsed

    # ------------------------------------------------------------- prompts
    @staticmethod
    def _build_planner_prompt(
        entry_json: str, context_json: str, voice_md: str
    ) -> str:
        """Assemble the deterministic planner prompt (no wall-clock / random)."""
        return (
            "Draft a marketing brief for this changelog entry as a single JSON "
            "object matching the MarketingBrief schema. Respond with ONLY the "
            "JSON object (no markdown, no prose, no code fence). Do NOT read "
            "files, use tools, or write anything — answer purely from the "
            "context below.\n\n"
            "Required keys: hook_template_per_channel (an object with x, "
            "linkedin, blog; each value one of: we_just_shipped, before_after, "
            "problem_aha, numbered_list, behind_the_scenes, 5_sec_value, "
            "social_proof), ctas (array of strings), video_beats (EXACTLY 4 "
            "objects with image_prompt, narration, duration_sec), carousel_beats "
            "(EXACTLY 4 objects with headline, body), has_stat_card (bool), "
            "has_code_screenshot (bool).\n\n"
            f"Picked entry (01_pick/entry.json):\n{entry_json}\n\n"
            f"Enriched context (02_enrich/context.json):\n{context_json}\n\n"
            f"Brand voice (03_brand/voice.md):\n{voice_md}\n"
        )

    @staticmethod
    def _build_guardian_prompt(
        draft_json: str, voice_md: str, proposal_json: str
    ) -> str:
        """Assemble the brand-guardian prompt that wraps the planner draft."""
        return (
            "Guard this draft MarketingBrief against the brand voice and visual "
            "identity. Return ONLY the corrected MarketingBrief as a single JSON "
            "object (same schema, guardian's version overrides the draft).\n\n"
            f"Draft brief:\n{draft_json}\n\n"
            f"Brand voice (03_brand/voice.md):\n{voice_md}\n\n"
            f"Approved palette (03_brand/proposal.json):\n{proposal_json}\n"
        )

    # ------------------------------------------------------------- run
    def run(self, project: Project) -> StageResult:
        """Chain planner → brand-guardian, validate, write ``04_plan/brief.json``.

        The brief is validated against :class:`MarketingBrief` BEFORE being
        written, so a length-violating brief (e.g. ``video_beats`` of 3) fails
        here and no partial ``brief.json`` is left on disk (TC-7.7).
        """
        entry_json = self._read_text(project, self.ENTRY_REL)
        context_json = self._read_text(project, self.CONTEXT_REL)
        voice_md = self._read_text(project, self.VOICE_REL)
        proposal_json = self._read_text(project, self.PROPOSAL_REL)

        # 1. planner drafts the brief — plain `claude -p` (the stock `planner`
        #    agent over-works a one-shot self-contained JSON task and times out).
        draft = self._invoke_subagent(
            None,
            self._build_planner_prompt(entry_json, context_json, voice_md),
            label="planner",
        )

        # 2. brand-guardian guards the draft; ITS output is final (TC-7.3).
        guarded = self._invoke_subagent(
            "brand-guardian",
            self._build_guardian_prompt(
                json.dumps(draft, sort_keys=True), voice_md, proposal_json
            ),
        )

        # Validate the guardian's output against the schema (HARD length rules).
        # A failure here raises before any file is written (TC-7.7).
        brief = MarketingBrief.model_validate(guarded)

        output_path = project.artifact_path(self.id, self.OUTPUT_FILENAME)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            dump_json_canonical(brief.model_dump(mode="json")),
            encoding="utf-8",
        )

        return StageResult(
            status=StageStatus.DONE,
            outputs=(Path(self.id) / self.OUTPUT_FILENAME,),
            metrics={
                "cost_usd": 0.0,
                "video_beats": len(brief.video_beats),
                "carousel_beats": len(brief.carousel_beats),
            },
        )
