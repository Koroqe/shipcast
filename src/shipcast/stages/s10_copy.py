"""Stage 10 — copy (X thread + LinkedIn long-form + blog markdown).

Turns the marketing brief + picked entry + enriched context + brand voice into
three publish-ready text artifacts via a SINGLE ``claude -p`` sub-agent
(``social-copywriter``). The agent returns a :class:`CopyBundle` JSON object with
``twitter_thread`` / ``linkedin`` / ``blog`` string fields; this stage writes the
three of them as ``10_copy/{twitter_thread,linkedin,blog}.md``.

Sub-agent error handling (mirrors ``s04_plan`` / ``s05_script``)
----------------------------------------------------------------
The ``social-copywriter`` call has a 300 s wall-clock budget. On timeout we raise
:class:`SubagentTimeout`, on a non-zero exit :class:`SubagentFailed`, and on
non-JSON / non-object stdout :class:`SubagentMalformedOutput` — each surfaces
through the dispatcher's FAILED transition (TC-13.5), leaving no ``.md`` files.

Channel-anatomy validation (HARD — before any write, TC-13.6)
-------------------------------------------------------------
``run`` validates the parsed sub-agent JSON against :class:`CopyBundle` BEFORE
writing anything: 3-8 numbered tweets each ≤ 280 chars (and no Markdown ``**``),
LinkedIn 600-1200 words, blog 1200-2000 words. A violation raises
``ValidationError`` -> the dispatcher records FAILED and NO partial ``.md`` files
are left on disk. The per-channel HOOK-OPENING rule (each file's first non-blank
line CONTAINS the brief's chosen hook for that channel, FR-12.4) needs the picked
entry, which the schema lacks, so ``run`` enforces it here via
:func:`shipcast.marketing.hooks.render` and raises
``SubagentMalformedOutput`` when a channel does not open with its hook.

``validate_outputs`` re-reads the three written ``.md`` files and re-asserts the
``CopyBundle`` length/structure bounds as defense-in-depth (the default
``validate_outputs`` cannot, because the on-disk artifacts are raw ``.md``, not
the bundle JSON).

voice.md read-path (Architect MAJOR Finding 1)
----------------------------------------------
This stage reads the CANONICAL ``03_brand/voice.md`` (the declared, hash-covered
copy ``s03_brand`` wrote) — NEVER the raw ``_brand/<slug>/`` pack (TC-13.9).

Cost
----
The sub-agent authenticates via the operator's local ``claude`` subscription and
incurs NO per-call USD cost, so this stage keeps the BaseStage default
``next_call_cost_usd`` of 0.0 and is never cost-gated.

Determinism (TC-21.3)
---------------------
The artifacts are the raw sub-agent strings; the stage wrapper contains no
``datetime.now()`` and no random id — non-determinism is confined to the single
sub-agent ``run()`` call.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import ValidationError

from shipcast.errors import (
    StageOutputInvalid,
    SubagentFailed,
    SubagentMalformedOutput,
    SubagentTimeout,
)
from shipcast.manifest import StageStatus
from shipcast.marketing import hooks
from shipcast.schemas import CopyBundle
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from collections.abc import Callable

    from shipcast.project import Project

#: Wall-clock budget for the social-copywriter sub-agent invocation.
_SUBAGENT_TIMEOUT_SEC: int = 300

#: CopyBundle field -> output filename + the brief channel key whose hook it
#: must open with. Order is the write order (deterministic).
_CHANNELS: tuple[tuple[str, str, str], ...] = (
    # (bundle_field, output_filename, brief_channel_key)
    ("twitter_thread", "twitter_thread.md", "x"),
    ("linkedin", "linkedin.md", "linkedin"),
    ("blog", "blog.md", "blog"),
)


class CopyStage(BaseStage):
    """Produce ``10_copy/{twitter_thread,linkedin,blog}.md`` via social-copywriter."""

    id: ClassVar[str] = "10_copy"
    requires: ClassVar[tuple[str, ...]] = ("04_plan",)
    output_schema: ClassVar[type[CopyBundle]] = CopyBundle
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Read the X thread: 3-8 numbered tweets, each ≤ 280 chars, Unicode bold "
        "not Markdown — and it opens with the chosen hook.",
        "Read the LinkedIn post: 600-1200 words, hook-first, → / ▸ bullets, a "
        "closing question, and ≤ 5 lowercase hashtags.",
        "Read the blog: 1200-2000 words, a TL;DR block, narrative arc, fenced "
        "code where relevant — and it opens with the chosen hook.",
        "Confirm every claim is grounded in the changelog entry — no invented "
        "features, numbers, or quotes.",
    )

    BRIEF_REL: ClassVar[str] = "04_plan/brief.json"
    ENTRY_REL: ClassVar[str] = "01_pick/entry.json"
    CONTEXT_REL: ClassVar[str] = "02_enrich/context.json"
    #: Canonical voice contract written by s03_brand (Finding-1 read-path).
    VOICE_REL: ClassVar[str] = "03_brand/voice.md"

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
        """Run one ``claude -p --agent <agent>`` call and parse its JSON stdout.

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
                    "--agent",
                    agent,
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
            parsed = json.loads(result.stdout)
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
    def _build_prompt(
        brief_json: str, entry_json: str, context_json: str, voice_md: str
    ) -> str:
        """Assemble the deterministic social-copywriter prompt."""
        return (
            "Write the three marketing-copy artifacts for this changelog entry as "
            "a single JSON object matching the CopyBundle schema.\n\n"
            "Required keys (each a non-empty string):\n"
            "- twitter_thread: 3-8 numbered tweets, one per line ('1/ ...'), each "
            "line <= 280 chars; Unicode mathematical bold for emphasis, NEVER "
            "Markdown '**bold**'.\n"
            "- linkedin: 600-1200 words; hook-first; '->'/'>' Unicode bullets (no "
            "Markdown '-'/'*'); a closing question; 3-5 lowercase hashtags.\n"
            "- blog: 1200-2000 words; opens with the hook then a TL;DR block; "
            "narrative arc; fenced code blocks with a language tag.\n\n"
            "Each channel MUST open (first non-blank line) with the hook template "
            "the brief chose for that channel (x / linkedin / blog).\n\n"
            f"Marketing brief (04_plan/brief.json):\n{brief_json}\n\n"
            f"Picked entry (01_pick/entry.json):\n{entry_json}\n\n"
            f"Enriched context (02_enrich/context.json):\n{context_json}\n\n"
            f"Brand voice (03_brand/voice.md):\n{voice_md}\n"
        )

    # ------------------------------------------------------------- hooks
    @staticmethod
    def _load_entry(entry_json: str) -> dict[str, object]:
        """Parse the picked-entry JSON into the mapping ``hooks.render`` expects.

        Tolerates a missing / unreadable entry by returning an empty mapping —
        ``hooks.render`` degrades gracefully to a generic phrase, so the hook
        substring check still has a concrete (non-empty) target.
        """
        try:
            parsed = json.loads(entry_json)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return parsed

    def _assert_hook_openings(
        self,
        bundle: CopyBundle,
        brief: dict[str, object],
        entry: dict[str, object],
    ) -> None:
        """Each channel's first non-blank line must CONTAIN its chosen hook.

        Raises :class:`SubagentMalformedOutput` (so the stage fails cleanly,
        leaving no ``.md`` files) when a channel does not open with the rendered
        hook for that channel (FR-12.4 / TC-13.2).
        """
        hook_map = brief.get("hook_template_per_channel")
        if not isinstance(hook_map, dict):
            raise SubagentMalformedOutput(
                "brief is missing hook_template_per_channel — cannot verify "
                "channel hook openings"
            )

        for field_name, _filename, channel in _CHANNELS:
            key = hook_map.get(channel)
            if not isinstance(key, str):
                raise SubagentMalformedOutput(
                    f"brief.hook_template_per_channel is missing channel {channel!r}"
                )
            expected = hooks.render(key, entry)
            body: str = getattr(bundle, field_name)
            first_line = next(
                (line for line in body.splitlines() if line.strip()), ""
            )
            if expected not in first_line:
                raise SubagentMalformedOutput(
                    f"{field_name} must open with the {channel!r} hook "
                    f"({key!r}); expected opening line to contain "
                    f"{expected!r}, got {first_line!r}"
                )

    # ------------------------------------------------------------- run
    def run(self, project: Project) -> StageResult:
        """Invoke social-copywriter, validate, write the three ``10_copy`` files.

        The :class:`CopyBundle` schema validation (tweet count + 280-char limit +
        no ``**``, LinkedIn/blog word counts) and the per-channel hook-opening
        check BOTH run BEFORE any file is written, so a violating bundle leaves no
        partial ``.md`` files on disk (TC-13.5 / TC-13.6).
        """
        brief_json = self._read_text(project, self.BRIEF_REL)
        entry_json = self._read_text(project, self.ENTRY_REL)
        context_json = self._read_text(project, self.CONTEXT_REL)
        voice_md = self._read_text(project, self.VOICE_REL)

        parsed = self._invoke_subagent(
            "social-copywriter",
            self._build_prompt(brief_json, entry_json, context_json, voice_md),
        )

        # HARD channel-anatomy validation (tweet count/length, word counts, no
        # Markdown bold). Raises ValidationError before any write (TC-13.6).
        bundle = CopyBundle.model_validate(parsed)

        # Per-channel hook-opening check (needs the picked entry + brief).
        brief = self._load_entry(brief_json)
        entry = self._load_entry(entry_json)
        self._assert_hook_openings(bundle, brief, entry)

        project.stage_dir(self.id).mkdir(parents=True, exist_ok=True)

        outputs: list[Path] = []
        for field_name, filename, _channel in _CHANNELS:
            body: str = getattr(bundle, field_name)
            (project.artifact_path(self.id, filename)).write_text(
                body, encoding="utf-8"
            )
            outputs.append(Path(self.id) / filename)

        return StageResult(
            status=StageStatus.DONE,
            outputs=tuple(outputs),
            metrics={"cost_usd": 0.0},
        )

    # ------------------------------------------------------------- validate
    def validate_outputs(self, project: Project, result: StageResult) -> None:
        """Re-read the three ``.md`` files and re-assert the CopyBundle bounds.

        The default ``validate_outputs`` only schema-checks when there is exactly
        ONE output AND that output is the JSON artifact; here the outputs are
        three raw ``.md`` files, so we reconstruct a :class:`CopyBundle` from
        their bytes and re-run its validators (length/structure) as
        defense-in-depth, plus the shared path-traversal guard.
        """
        self._validate_output_paths(project, result)

        bodies: dict[str, str] = {}
        for field_name, filename, _channel in _CHANNELS:
            full = project.artifact_path(self.id, filename)
            if not full.is_file():
                raise StageOutputInvalid(
                    f"stage {self.id!r} declared output {filename!r} but file is "
                    f"missing at {full}"
                )
            bodies[field_name] = full.read_text(encoding="utf-8")

        try:
            CopyBundle.model_validate(bodies)
        except ValidationError as exc:
            raise StageOutputInvalid(
                f"stage {self.id!r} outputs failed CopyBundle validation: {exc}"
            ) from exc
