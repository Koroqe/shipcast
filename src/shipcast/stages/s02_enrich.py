"""Stage 02 — enrich.

Turns the picked changelog entry (``01_pick/entry.json``) into a rich
marketing-narrative context by combining three signals about the change:

1. **Repo signals** — ``gh pr list`` and ``git log --stat`` run against the
   target ``repo_path`` (read-only; shipcast never writes into the target
   repo). Produces ``pr_links`` (merged-PR URLs) and ``diff_stats`` (aggregate
   files/insertions/deletions numbers).
2. **Real-app screenshots** — IF ``input.yaml.live_url`` is present, the
   operator-authored ``feature_walkthrough`` is replayed via a Playwright
   client to capture ``02_enrich/screenshots/*.png``. When ``live_url`` is
   OMITTED the entire sub-step is SKIPPED (and logged) and the stage proceeds
   with no screenshots (UC-3-A1).
3. **Multimodal narrative** — ``gemini_client.multimodal(prompt, screenshots)``
   folds the entry text + diff stats (+ screenshots when present) into a single
   marketing narrative. The ``ba-analyst`` sub-agent (`claude -p`, 300 s) adds
   high-level framing.

Architect MAJOR Finding 3 — single source of truth
---------------------------------------------------
The narrative is stored in EXACTLY ONE on-disk location:
``02_enrich/context.json``'s ``narrative`` field. This stage deliberately does
NOT also write a sibling ``narrative.md``. A second copy would be an
undeclared, un-hash-covered duplicate that could silently drift from
``context.json``. ``context.json`` is the stage's ONLY declared output, so
``compute_outputs_hash`` covers every byte of the narrative and the
``approve`` edit-detection path sees any operator change to it
(TC-5.9 / TC-20.4).

Forward-dependency note (Playwright — Slice 8)
----------------------------------------------
The concrete ``PlaywrightClient`` lands in Slice 8. This stage therefore
obtains the playwright client from the injected ``clients_factory`` and
duck-types its interface (it only calls ``screenshot_feature``). The default
clients factory returns ``None`` for ``playwright`` until Slice 8 wires the
real client, so the stage runs (screenshots skipped) when no playwright client
is available. Tests inject a mock bundle with a canned playwright client.

Lazy clients
------------
All external clients (Gemini, Playwright, the ``claude`` subprocess) are
constructed inside ``run()`` via ``self._clients_factory(project)`` — never at
import time. This keeps the heavy ``requests`` import out of CLI startup
(import-purity test) and lets stage tests inject mocks.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from shipcast.cost import GEMINI_MULTIMODAL_CALL_USD
from shipcast.errors import (
    StageInputMissing,
    SubagentFailed,
    SubagentMalformedOutput,
    SubagentTimeout,
)
from shipcast.manifest import StageStatus, dump_json_canonical
from shipcast.schemas import EnrichedContext, InputYaml
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from collections.abc import Callable

    from shipcast.project import Project

#: Wall-clock budget for the `ba-analyst` sub-agent invocation.
_SUBAGENT_TIMEOUT_SEC: int = 300


# --------------------------------------------------------------------------- #
# Clients bundle Protocol (structural; mocked in tests)
# --------------------------------------------------------------------------- #


@runtime_checkable
class _GeminiLike(Protocol):
    def multimodal(self, prompt: str, images: list[Path]) -> str: ...


@runtime_checkable
class _PlaywrightLike(Protocol):
    def screenshot_feature(
        self, url: str, walkthrough: list[dict[str, Any]]
    ) -> list[Path]: ...


class _ClientsBundle(Protocol):
    """Structural type for the bundle returned by ``clients_factory``.

    ``playwright`` is ``None`` until Slice 8 wires the real client — the stage
    skips screenshots in that case (and also when ``live_url`` is omitted).
    """

    @property
    def gemini(self) -> _GeminiLike: ...

    @property
    def playwright(self) -> _PlaywrightLike | None: ...


def _default_clients_factory(project: Project) -> _ClientsBundle:
    """Construct the real client bundle lazily inside ``run()``.

    Imports the heavy ``GeminiClient`` (which itself imports ``requests`` only
    inside its methods) here, NOT at module top, to preserve import-purity.
    ``playwright`` is ``None`` until Slice 8 lands ``PlaywrightClient``.
    """
    from shipcast.clients.gemini_client import GeminiClient

    gemini = GeminiClient(api_key=project.settings.gemini_api_key)

    class _Bundle:
        def __init__(self) -> None:
            self.gemini: _GeminiLike = gemini
            self.playwright: _PlaywrightLike | None = None

    return _Bundle()


class EnrichStage(BaseStage):
    """Enrich the picked entry into ``02_enrich/context.json`` (Finding-3 sole copy)."""

    id: ClassVar[str] = "02_enrich"
    requires: ClassVar[tuple[str, ...]] = ("01_pick",)
    output_schema: ClassVar[type[EnrichedContext]] = EnrichedContext
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Read the narrative — confirm it frames the change accurately and on-brand.",
        "Verify the PR links and diff stats correspond to the picked changelog entry.",
        "If screenshots were captured, confirm they show the real feature (not an error page).",
    )

    OUTPUT_FILENAME: ClassVar[str] = "context.json"
    SCREENSHOTS_DIRNAME: ClassVar[str] = "screenshots"

    def __init__(
        self,
        *,
        clients_factory: Callable[[Project], _ClientsBundle] | None = None,
        subprocess_run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._clients_factory: Callable[[Project], _ClientsBundle] = (
            clients_factory or _default_clients_factory
        )
        # Indirection so tests can inject a fake `claude -p` without patching the
        # global `subprocess` module. Defaults to the real `subprocess.run`.
        self._subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = (
            subprocess_run or subprocess.run
        )

    # ------------------------------------------------------------- cost gate
    def next_call_cost_usd(self, project: Project) -> float:
        """The Gemini multimodal call this stage makes (one per run)."""
        return GEMINI_MULTIMODAL_CALL_USD

    # ------------------------------------------------------------- input read
    def _load_input(self, project: Project) -> InputYaml:
        """Read + validate ``input.yaml`` (same SSRF/path defenses as s01)."""
        import yaml

        if not project.input_path.is_file():
            raise StageInputMissing(
                f"stage {self.id!r} requires {project.input_path} to exist"
            )
        raw = project.input_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError(
                f"{project.input_path} must contain a YAML mapping, got {type(data).__name__}"
            )
        cleaned = {k: v for k, v in data.items() if v is not None}
        return InputYaml.model_validate(cleaned)

    # ------------------------------------------------------------- repo signals
    def _collect_repo_signals(self, repo_path: Path) -> tuple[list[str], dict[str, object]]:
        """Run ``gh pr list`` + ``git log --stat`` (read-only) against the repo.

        Returns ``(pr_links, diff_stats)``. Both subprocess calls are tolerant:
        a non-zero exit (e.g. ``gh`` unauthenticated, repo not a git repo) does
        NOT fail the stage — the corresponding signal is simply empty. The
        enrichment narrative degrades gracefully rather than aborting the run.
        """
        pr_links = self._collect_pr_links(repo_path)
        diff_stats = self._collect_diff_stats(repo_path)
        return pr_links, diff_stats

    def _collect_pr_links(self, repo_path: Path) -> list[str]:
        try:
            result = self._subprocess_run(
                ["gh", "pr", "list", "--state", "merged", "--limit", "20", "--json", "url"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        links: list[str] = []
        if isinstance(rows, list):
            for row in rows:
                url = row.get("url") if isinstance(row, dict) else None
                if isinstance(url, str) and url:
                    links.append(url)
        return links

    def _collect_diff_stats(self, repo_path: Path) -> dict[str, object]:
        try:
            result = self._subprocess_run(
                ["git", "log", "-1", "--stat", "--format="],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {}
        if result.returncode != 0:
            return {}
        return self._parse_diffstat(result.stdout)

    @staticmethod
    def _parse_diffstat(text: str) -> dict[str, object]:
        """Parse the trailing summary line of ``git log --stat`` output.

        Looks for the ``N files changed, M insertions(+), K deletions(-)``
        summary line and returns its numbers. Missing parts default to 0.
        """
        files = insertions = deletions = 0
        for line in text.splitlines():
            stripped = line.strip()
            if "changed" not in stripped:
                continue
            for chunk in stripped.split(","):
                chunk = chunk.strip()
                head = chunk.split(" ", 1)[0]
                if not head.isdigit():
                    continue
                n = int(head)
                if "file" in chunk:
                    files = n
                elif "insertion" in chunk:
                    insertions = n
                elif "deletion" in chunk:
                    deletions = n
        return {
            "files_changed": files,
            "insertions": insertions,
            "deletions": deletions,
        }

    # ------------------------------------------------------------- screenshots
    def _capture_screenshots(
        self,
        project: Project,
        spec: InputYaml,
        clients: _ClientsBundle,
    ) -> list[Path]:
        """Capture feature-walkthrough screenshots when ``live_url`` is present.

        Returns project-relative paths to the captured PNGs. Skips (returning
        ``[]``) when ``live_url`` is omitted OR no playwright client is
        available (Slice-8 forward dependency). A ``PlaywrightTimeout`` raised
        by the client propagates and fails the stage (TC-5.8).
        """
        import logging

        logger = logging.getLogger("shipcast")
        if spec.live_url is None:
            logger.info(
                "live_url omitted — skipping Playwright screenshot capture",
                extra={"event": "enrich_screenshots_skipped", "stage_id": self.id},
            )
            return []
        playwright = clients.playwright
        if playwright is None:
            logger.info(
                "no Playwright client available — skipping screenshot capture",
                extra={"event": "enrich_no_playwright", "stage_id": self.id},
            )
            return []

        walkthrough = [step.model_dump(mode="json") for step in (spec.feature_walkthrough or [])]
        abs_paths = playwright.screenshot_feature(str(spec.live_url), walkthrough)

        screenshots_dir = project.stage_dir(self.id) / self.SCREENSHOTS_DIRNAME
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        rel_paths: list[Path] = []
        for src in abs_paths:
            src = Path(src)
            dest = screenshots_dir / src.name
            if src.resolve() != dest.resolve():
                dest.write_bytes(src.read_bytes())
            rel_paths.append(dest.relative_to(project.path))
        return rel_paths

    # ------------------------------------------------------------- ba-analyst
    def _run_ba_analyst(self, prompt: str) -> dict[str, object]:
        """Invoke the ``ba-analyst`` sub-agent via ``claude -p`` and parse JSON.

        Raises:
            SubagentTimeout: the subprocess exceeded the 300 s budget.
            SubagentFailed: the subprocess exited non-zero (stderr captured).
            SubagentMalformedOutput: stdout was not valid JSON.
        """
        try:
            # Plain `claude -p` (default agent) for this bounded one-shot
            # framing call. The stock `ba-analyst` agent is a use-case-doc
            # writer whose system prompt drives it to explore the repo and emit
            # markdown — it over-works a one-shot JSON task and blows the
            # timeout. The framing prompt is fully self-contained, so a plain
            # call is the right-sized tool.
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
                f"ba-analyst exceeded {_SUBAGENT_TIMEOUT_SEC}s timeout"
            ) from exc

        if result.returncode != 0:
            raise SubagentFailed("ba-analyst", result.returncode, result.stderr or "")

        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SubagentMalformedOutput(
                f"ba-analyst stdout was not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise SubagentMalformedOutput(
                f"ba-analyst JSON must be an object, got {type(parsed).__name__}"
            )
        return parsed

    # ------------------------------------------------------------- prompts
    @staticmethod
    def _build_narrative_prompt(
        entry: dict[str, object],
        pr_links: list[str],
        diff_stats: dict[str, object],
        has_screenshots: bool,
    ) -> str:
        """Assemble the deterministic multimodal prompt (no wall-clock / random)."""
        lines = [
            "You are writing a marketing narrative for a software changelog entry.",
            f"Entry name: {entry.get('name', '')}",
            f"Summary: {entry.get('summary', '')}",
            f"Details: {entry.get('details', '')}",
            f"Merged PRs: {', '.join(pr_links) if pr_links else '(none found)'}",
            f"Diff stats: {json.dumps(diff_stats, sort_keys=True)}",
        ]
        if has_screenshots:
            lines.append("Attached screenshots show the feature in the live app.")
        return "\n".join(lines)

    @staticmethod
    def _build_ba_prompt(entry: dict[str, object]) -> str:
        return (
            "Provide high-level marketing framing for this changelog entry. "
            "Respond with ONLY a single compact JSON object (no markdown, no "
            "prose, no code fence). Do NOT read files, use tools, or write "
            "anything — answer purely from the text below.\n"
            f"Entry name: {entry.get('name', '')}\n"
            f"Summary: {entry.get('summary', '')}\n"
            f"Details: {entry.get('details', '')}"
        )

    # ------------------------------------------------------------- run
    def run(self, project: Project) -> StageResult:
        """Collect repo signals + screenshots, generate the narrative, write context.json.

        Determinism: the artifact JSON wrapper contains no ``datetime.now()`` and
        no random id (TC-21.3); non-determinism is confined to the LLM call text.
        """
        spec = self._load_input(project)
        entry_path = project.artifact_path("01_pick", "entry.json")
        entry: dict[str, object] = json.loads(entry_path.read_text(encoding="utf-8"))

        clients = self._clients_factory(project)

        # (a) repo signals
        pr_links, diff_stats = self._collect_repo_signals(spec.repo_path)

        # (b) screenshots (skipped when live_url omitted OR no playwright client)
        screenshots = self._capture_screenshots(project, spec, clients)

        # (c) multimodal narrative — GeminiRateLimited (429) propagates → FAILED
        narrative_prompt = self._build_narrative_prompt(
            entry, pr_links, diff_stats, has_screenshots=bool(screenshots)
        )
        screenshot_abs = [project.path / p for p in screenshots]
        narrative = clients.gemini.multimodal(narrative_prompt, screenshot_abs)

        # ba-analyst framing (SubagentTimeout / SubagentFailed / malformed → FAILED)
        ba_framing = self._run_ba_analyst(self._build_ba_prompt(entry))

        context = EnrichedContext(
            pr_links=pr_links,
            diff_stats=diff_stats,
            narrative=narrative,
            screenshots=[str(p) for p in screenshots],
            ba_framing=ba_framing,
        )

        output_path = project.artifact_path(self.id, self.OUTPUT_FILENAME)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            dump_json_canonical(context.model_dump(mode="json")),
            encoding="utf-8",
        )

        # Finding 3: context.json is the SOLE declared output; the narrative
        # lives only in its `narrative` field. No `narrative.md` is written.
        outputs: list[Path] = [Path(self.id) / self.OUTPUT_FILENAME]
        outputs.extend(screenshots)

        return StageResult(
            status=StageStatus.DONE,
            outputs=tuple(outputs),
            metrics={
                "cost_usd": GEMINI_MULTIMODAL_CALL_USD,
                "pr_count": len(pr_links),
                "screenshot_count": len(screenshots),
            },
        )

    # ------------------------------------------------------------- validate
    def validate_outputs(self, project: Project, result: StageResult) -> None:
        """Validate path safety + the context.json schema (screenshots are PNGs).

        The default ``validate_outputs`` only schema-checks when there is exactly
        ONE output; this stage may also emit screenshot PNGs, so we validate
        ``context.json`` against ``EnrichedContext`` explicitly and run the
        shared path-traversal guard over every declared output.
        """
        from pydantic import ValidationError

        from shipcast.errors import StageOutputInvalid

        self._validate_output_paths(project, result)
        context_rel = Path(self.id) / self.OUTPUT_FILENAME
        full = (project.path / context_rel).resolve()
        try:
            data = json.loads(full.read_text(encoding="utf-8"))
            EnrichedContext.model_validate(data)
        except json.JSONDecodeError as exc:
            raise StageOutputInvalid(
                f"stage {self.id!r} output {full} is not valid JSON: {exc}"
            ) from exc
        except ValidationError as exc:
            raise StageOutputInvalid(
                f"stage {self.id!r} output {full} failed schema validation: {exc}"
            ) from exc
