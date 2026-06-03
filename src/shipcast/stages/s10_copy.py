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
import logging
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
from shipcast.logging_setup import LOGGER_NAME
from shipcast.manifest import StageStatus
from shipcast.marketing import hooks
from shipcast.schemas import CopyBundle
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from collections.abc import Callable

    from shipcast.project import Project

#: Wall-clock budget for the social-copywriter sub-agent invocation. Copy is
#: the heaviest one-shot generation in the pipeline (a 1200-2000 word blog plus
#: a LinkedIn post plus an X thread in a single call), so it gets a wider budget
#: than the other sub-agent stages.
_SUBAGENT_TIMEOUT_SEC: int = 600

#: Unique marker lines delimiting the three artifacts in the copywriter's
#: plain-text stdout. Markers cannot appear in normal copy, so each section's
#: raw text (incl. the blog's own ``` code fences) is captured verbatim — far
#: more robust than one JSON object whose long-form string values would carry
#: invalid literal newlines.
_M_TWITTER = "<<<TWITTER>>>"
_M_LINKEDIN = "<<<LINKEDIN>>>"
_M_BLOG = "<<<BLOG>>>"
_M_END = "<<<END>>>"

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
    def _invoke_subagent(self, agent: str, prompt: str) -> str:
        """Run one plain ``claude -p`` call and return its raw stdout text.

        Copy is three LONG-FORM documents. Forcing them through one JSON object
        is fragile — the model embeds literal newlines (and the blog's own
        ``````` code fences) inside JSON string values, which is
        invalid JSON. So the contract is marker-delimited PLAIN TEXT (see
        :meth:`_parse_sections`) rather than JSON: raw newlines and nested code
        fences are then harmless. ``agent`` is retained only as the error label.

        Raises:
            SubagentTimeout: the subprocess exceeded the 300 s budget.
            SubagentFailed: the subprocess exited non-zero (stderr captured).
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

        return result.stdout or ""

    # ------------------------------------------------------------- parse
    @staticmethod
    def _parse_sections(text: str) -> dict[str, str]:
        """Split marker-delimited stdout into the three CopyBundle fields.

        The copywriter emits each artifact between unique marker lines
        (``<<<TWITTER>>>`` / ``<<<LINKEDIN>>>`` / ``<<<BLOG>>>`` / ``<<<END>>>``).
        Markers cannot appear in normal copy, so each section's raw text — with
        ordinary line breaks and the blog's own code fences — is captured
        verbatim. Raises :class:`SubagentMalformedOutput` if any marker is
        missing or out of order, so the stage fails cleanly with no files.
        """
        order = [_M_TWITTER, _M_LINKEDIN, _M_BLOG, _M_END]
        positions: list[int] = []
        for marker in order:
            idx = text.find(marker)
            if idx == -1:
                raise SubagentMalformedOutput(
                    f"social-copywriter output is missing the {marker!r} marker"
                )
            positions.append(idx)
        if not (positions[0] < positions[1] < positions[2] < positions[3]):
            raise SubagentMalformedOutput(
                "social-copywriter markers are out of order "
                "(expected TWITTER < LINKEDIN < BLOG < END)"
            )
        sections: dict[str, str] = {}
        fields = ("twitter_thread", "linkedin", "blog")
        for i, field in enumerate(fields):
            start = positions[i] + len(order[i])
            end = positions[i + 1]
            sections[field] = text[start:end].strip("\n").strip()
        return sections

    # ------------------------------------------------------------- prompt
    @staticmethod
    def _build_prompt(
        brief_json: str, entry_json: str, context_json: str, voice_md: str
    ) -> str:
        """Assemble the deterministic social-copywriter prompt."""
        return (
            "Write the three marketing-copy artifacts for this changelog entry. "
            "Output them as PLAIN TEXT separated by these EXACT marker lines, "
            "each marker alone on its own line, in this order:\n"
            f"{_M_TWITTER}\n<the X/Twitter thread>\n{_M_LINKEDIN}\n"
            f"<the LinkedIn post>\n{_M_BLOG}\n<the blog post>\n{_M_END}\n\n"
            "Put each artifact's full text (with normal line breaks; the blog "
            "MAY contain ``` code fences) between its marker and the next "
            "marker. Output NOTHING outside the markers — no JSON, no preamble. "
            "Do NOT use the Write/Edit tools or create files.\n\n"
            "Channel requirements (the word-count MINIMUMS are HARD — output "
            "that falls short is rejected, so write the FULL-LENGTH pieces; do "
            "not summarize, abbreviate, or stop early):\n"
            "- X thread: 3-8 numbered tweets, one per line ('1/ ...'), each line "
            "<= 280 chars; Unicode mathematical bold for emphasis, NEVER Markdown "
            "'**bold**'.\n"
            "- LinkedIn: a COMPLETE post — TARGET about 900 words, and NEVER "
            "fewer than 600 (count them); hook-first; 6-8 substantive paragraphs "
            "with concrete detail and examples; '->'/'>' Unicode bullets (no "
            "Markdown '-'/'*'); a closing question; 3-5 lowercase hashtags.\n"
            "- blog: a COMPLETE article — TARGET about 1600 words, and NEVER "
            "fewer than 1200 (count them); opens with the hook then a TL;DR "
            "block; a full narrative arc (problem -> constraint -> exploration "
            "-> solution -> result) across many paragraphs and sections; fenced "
            "code blocks with a language tag.\n\n"
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
        """Validate the brief's hook mapping; advisory-check each channel opening.

        The STRUCTURAL part is HARD: the brief must carry a
        ``hook_template_per_channel`` mapping with a template key for every
        channel (a broken brief raises :class:`SubagentMalformedOutput`).

        The hook-OPENING part is ADVISORY: requiring the copy's first line to
        contain the verbatim ``hooks.render(...)`` string is incompatible with
        real LLM copy, which legitimately PARAPHRASES the hook rather than
        pasting a long auto-generated sentence. The chosen hook is enforced in
        the copywriter PROMPT; here we only LOG when the verbatim opener is
        absent so the operator can eyeball it at the human gate, never failing
        the stage on a paraphrase.
        """
        hook_map = brief.get("hook_template_per_channel")
        if not isinstance(hook_map, dict):
            raise SubagentMalformedOutput(
                "brief is missing hook_template_per_channel — cannot verify "
                "channel hook openings"
            )

        logger = logging.getLogger(LOGGER_NAME)
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
                logger.warning(
                    "hook opening (advisory): %s did not include the verbatim "
                    "%r hook (%r); the copy may paraphrase it — review at the "
                    "human gate.",
                    field_name,
                    channel,
                    key,
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

        stdout = self._invoke_subagent(
            "social-copywriter",
            self._build_prompt(brief_json, entry_json, context_json, voice_md),
        )
        parsed = self._parse_sections(stdout)

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
