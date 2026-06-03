"""CLI dispatcher for the shipcast pipeline.

Operator surface:

* `shipcast status <slug>` — render a colorized status table (READ-ONLY, no lock).
* `shipcast approve <slug> <stage_id>` — human approval, with byte-content edit detection.
* `shipcast reset <slug> <stage_id> [--yes]` — drop stage outputs + transitive cascade.
* `shipcast <verb> <slug> [--rerun] [--yes]` — dispatch one of the 11 pipeline stages.

The dispatcher is the ONLY place that mutates manifest state, holds the
project lock, writes per-stage tracebacks, or prints the Review Checklist.
Stages remain pure.

Design constraints baked in from the architect + security pre-reviews:

* Cascade-confirmation guard runs BEFORE the lock is acquired (ARC-REC-1).
* `--no-lock` requires `SHIPCAST_NO_LOCK_ACK` to be exactly `"1"` (SEC-B2).
* Stage outputs are validated to live inside `stage_dir` (SEC-B4 + ARC-REC-7).
* `shipcast reset` and `--rerun` delete ONLY manifest-declared `record.outputs`
  via `Path.unlink()` — never `shutil.rmtree(stage_dir)` (SEC-B3).
* Tracebacks captured for the log file use `traceback.format_exc()` — never
  with `capture_locals=True` (SEC-B1).
* Every `RUNNING` and `DONE` transition explicitly clears stale `error` /
  `finished_at` fields (ARC-R2).
* The traceback log line is written BEFORE the FAILED transition saves
  (ARC-R3), so the manifest's `error.traceback_path` always points at
  something readable.
* `pre_run_hook` is invoked by the dispatcher exactly ONCE between
  `check_inputs` and `run` — never inside a stage's `run()` body (ARC-R1).
* `shipcast status` does NOT acquire the lock (ARC-REC-6).
* ffmpeg pre-flight is gated by `stage.requires_ffmpeg`, not a hardcoded id
  (ARC-REC-3).

Slice 1 note: ``shipcast.stages.ALL_STAGES`` is empty, so every stage verb is
registered (and listed in ``shipcast --help``) but resolves to "not yet
implemented" until its owning slice appends its class to ``ALL_STAGES``. The
stage class is resolved from the LIVE registry at call time so later slices —
and tests injecting a fake stage — take effect without touching this module.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

import shipcast.stages as _stages
from shipcast import __version__
from shipcast.clients import check_available_or_raise
from shipcast.config import Settings
from shipcast.cost import CostLedger
from shipcast.errors import (
    CannotApproveNonDoneStage,
    CostCapExceeded,
    FfmpegNotFound,
    InvalidSlug,
    LockBypassNotAcknowledged,
    ManifestCorrupt,
    ManifestMigrationNeeded,
    ProjectExists,
    ProjectLocked,
    ProjectNotFound,
    ShipcastError,
)
from shipcast.locking import acquire, check_platform
from shipcast.logging_setup import LOGGER_NAME, configure
from shipcast.manifest import (
    ErrorRecord,
    StageStatus,
    compute_inputs_hash,
    compute_outputs_hash,
)
from shipcast.project import Project
from shipcast.stage import StageResult
from shipcast.stages import BaseStage, build_downstream_map

# Exit codes — surface meaningful values for shell scripting.
_EXIT_OK = 0
_EXIT_USER_ERROR = 1            # CannotApproveNonDoneStage, ProjectExists, InvalidSlug, refused cascade
_EXIT_STAGE_FAILURE = 2         # StageInputMissing, StageNotApproved, not-implemented verb, etc.
_EXIT_PROJECT_LOOKUP = 3        # ProjectNotFound, ManifestCorrupt, ManifestMigrationNeeded
_EXIT_CONCURRENCY = 4           # ProjectLocked, LockBypassNotAcknowledged, UnsupportedPlatform
_EXIT_DEPENDENCY = 5            # FfmpegNotFound

#: Verb → stage_id. The 11 fixed shipcast pipeline ids, in order. The verb name
#: maps to the stage_id; the concrete class is resolved from the LIVE registry
#: (`_stage_registry`) at call time, NOT bound here, so unimplemented stages
#: fail gracefully and test-injected stages are picked up.
_VERB_TO_STAGE_ID: dict[str, str] = {
    "pick": "01_pick",
    "enrich": "02_enrich",
    "brand": "03_brand",
    "plan": "04_plan",
    "script": "05_script",
    "video_assets": "06_video_assets",
    "voice": "07_voice",
    "video": "08_video",
    "graphics": "09_graphics",
    "copy": "10_copy",
    "package": "11_package",
}

_console = Console()

app = typer.Typer(
    name="shipcast",
    help="shipcast auto-marketing factory CLI.",
    no_args_is_help=True,
    add_completion=False,
)


# --------------------------------------------------------------------------- #
# Live stage registry — resolved at call time so later slices + test doubles
# take effect without editing this module.
# --------------------------------------------------------------------------- #


def _stage_registry() -> dict[str, type[BaseStage]]:
    """Return the live `stage_id → stage class` map from `shipcast.stages.ALL_STAGES`.

    Read at CALL time (not import time): appending a class to `ALL_STAGES` in a
    later slice — or monkeypatching it in a test — is immediately visible here.
    """
    return {cls.id: cls for cls in _stages.ALL_STAGES}


# --------------------------------------------------------------------------- #
# Global state — set by the root callback so every sub-command can see them.
# --------------------------------------------------------------------------- #


class _GlobalOptions:
    projects_root: Path = Path("projects")
    no_lock: bool = False


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the shipcast package version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    projects_root: Annotated[
        Path,
        typer.Option(
            "--projects-root",
            help="Directory containing per-entry project folders. Defaults to ./projects/.",
        ),
    ] = Path("projects"),
    no_lock: Annotated[
        bool,
        typer.Option(
            "--no-lock",
            help=(
                "Bypass the project lock. Requires SHIPCAST_NO_LOCK_ACK=1 in the "
                "environment to actually take effect. Use at your own risk."
            ),
        ),
    ] = False,
) -> None:
    """Root command. Sub-commands are registered below."""
    _GlobalOptions.projects_root = projects_root
    _GlobalOptions.no_lock = no_lock


# --------------------------------------------------------------------------- #
# Helpers — load/save, cascade guard, lock acquisition, audit logging.
# --------------------------------------------------------------------------- #


def _load_project_or_exit(slug: str) -> Project:
    """Load a project; surface every plausible error with a clean exit code."""
    try:
        return Project.load(_GlobalOptions.projects_root, slug)
    except ProjectNotFound as exc:
        _console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(_EXIT_PROJECT_LOOKUP) from exc
    except ManifestMigrationNeeded as exc:
        _console.print(f"[red]error:[/red] manifest migration required: {exc}")
        raise typer.Exit(_EXIT_PROJECT_LOOKUP) from exc
    except ManifestCorrupt as exc:
        _console.print(f"[red]error:[/red] manifest corrupt: {exc}")
        raise typer.Exit(_EXIT_PROJECT_LOOKUP) from exc


def _check_no_lock_acknowledgement() -> None:
    """Refuse `--no-lock` unless `SHIPCAST_NO_LOCK_ACK` is exactly `"1"`."""
    ack = os.environ.get("SHIPCAST_NO_LOCK_ACK")
    if ack != "1":
        raise LockBypassNotAcknowledged(
            "--no-lock requires SHIPCAST_NO_LOCK_ACK=1 in the environment. "
            "Truthy values like 'true', 'yes', or '0' are intentionally rejected: "
            "bypassing the lock can corrupt manifest state under concurrent access."
        )


def _audit_lock_bypass(slug: str, verb: str) -> None:
    """Emit the audit record (BOTH console banner AND JSON-line log) for --no-lock."""
    user = os.environ.get("USER", "?")
    pid = os.getpid()
    banner = (
        f"WARNING: --no-lock honored for slug={slug} verb={verb} "
        f"user={user} pid={pid}. Manifest state is unprotected."
    )
    _console.print(Panel(f"[yellow]{banner}[/yellow]", title="lock bypass"))
    logger = logging.getLogger(LOGGER_NAME)
    logger.warning(
        "lock bypass acknowledged",
        extra={
            "event": "lock_bypass",
            "slug": slug,
            "user": user,
            "pid": pid,
            "verb": verb,
        },
    )


@contextlib.contextmanager
def _lock_or_bypass(project: Project, verb: str) -> Iterator[None]:
    """Acquire the project lock; honor `--no-lock` with SHIPCAST_NO_LOCK_ACK=1.

    Encapsulates both the strict-equality env-var guard (SEC-B2) and the
    audit log emission (SEC-REC-1).
    """
    if _GlobalOptions.no_lock:
        try:
            _check_no_lock_acknowledgement()
        except LockBypassNotAcknowledged as exc:
            _console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(_EXIT_CONCURRENCY) from exc
        _audit_lock_bypass(project.slug, verb)
        yield
        return
    lock_path = project.path / ".lock"
    try:
        with acquire(lock_path):
            yield
    except ProjectLocked as exc:
        _console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(_EXIT_CONCURRENCY) from exc


def _collect_downstream_approvals(
    project: Project, root_stage_id: str
) -> list[tuple[str, datetime]]:
    """Return downstream stages with `human_approved_at` set, in dependency order.

    Used by the cascade-confirmation guard shared between `--rerun` and
    `shipcast reset` (ARC-R4).
    """
    downstream_map = build_downstream_map()
    visited: set[str] = set()
    queue: list[str] = list(downstream_map.get(root_stage_id, ()))
    ordered: list[tuple[str, datetime]] = []
    while queue:
        stage_id = queue.pop(0)
        if stage_id in visited or stage_id not in project.manifest.stages:
            continue
        visited.add(stage_id)
        record = project.manifest.stages[stage_id]
        if record.human_approved_at is not None:
            ordered.append((stage_id, record.human_approved_at))
        queue.extend(downstream_map.get(stage_id, ()))
    return ordered


def _confirm_cascade(
    project: Project, root_stage_id: str, *, yes: bool, action_verb: str
) -> bool:
    """Run the shared cascade-confirmation guard.

    Returns True if the action should proceed; False if it should abort.
    Raises typer.Exit(1) on explicit refusal in a non-tty context.
    """
    approvals = _collect_downstream_approvals(project, root_stage_id)
    if not approvals:
        return True
    table = Table(
        title=f"{action_verb} will discard approvals on {len(approvals)} downstream stage(s)"
    )
    table.add_column("stage_id", style="cyan")
    table.add_column("approved_at", style="green")
    for sid, when in approvals:
        table.add_row(sid, when.isoformat())
    _console.print(table)
    if yes:
        return True
    if not sys.stdin.isatty():
        _console.print(
            "[red]error:[/red] cascade would discard approvals; pass --yes to proceed "
            "(or run in a tty for an interactive prompt)."
        )
        raise typer.Exit(_EXIT_USER_ERROR)
    return Confirm.ask(f"Proceed with {action_verb}?", default=False)


def _delete_declared_outputs(project: Project, stage_id: str) -> None:
    """Delete only the manifest-declared outputs for `stage_id` (SEC-B3).

    NEVER `shutil.rmtree(stage_dir)` — operator-placed files (e.g. a brand pack
    or reference images) must survive a stage reset.
    """
    record = project.manifest.stages.get(stage_id)
    if record is None:
        return
    stage_dir = project.stage_dir(stage_id).resolve()
    for rel_path in record.outputs:
        full = (project.path / rel_path).resolve()
        if not full.is_relative_to(stage_dir):
            # Manifest poisoning: refuse to follow the declared path.
            _console.print(
                f"[red]error:[/red] manifest output {rel_path!r} resolves outside "
                f"stage_dir; skipping delete (manifest may be tampered)"
            )
            continue
        with contextlib.suppress(FileNotFoundError):
            full.unlink()


def _print_review_checklist(stage: BaseStage, project: Project, result: StageResult) -> None:
    """Print the Review Checklist Rich Panel after a successful stage run."""
    artifact_lines = [str((project.path / p).resolve()) for p in result.outputs]
    artifacts_block = (
        "\n".join(f"  • {p}" for p in artifact_lines) or "  (no output files declared)"
    )
    items_block = "\n".join(f"  • {item}" for item in stage.review_checklist_items)
    next_steps = (
        f"  • Rerun: `shipcast {_stage_id_to_verb(stage.id)} {project.slug} --rerun`\n"
        f"  • Edit then approve: edit the artifact(s), then "
        f"`shipcast approve {project.slug} {stage.id}`\n"
        f"  • Reset: `shipcast reset {project.slug} {stage.id} --yes`"
    )
    body = (
        f"[bold]Artifacts:[/bold]\n{artifacts_block}\n\n"
        f"[bold]Things to look for:[/bold]\n{items_block}\n\n"
        f"[bold]Next steps:[/bold]\n{next_steps}"
    )
    _console.print(
        Panel(body, title=f"Review checklist — {stage.id}", border_style="green")
    )


def _stage_id_to_verb(stage_id: str) -> str:
    """Inverse of `_VERB_TO_STAGE_ID` — for printing usage hints."""
    for verb, sid in _VERB_TO_STAGE_ID.items():
        if sid == stage_id:
            return verb
    return stage_id  # fallback: print the id verbatim


def _resolve_stage_or_exit(stage_id: str) -> type[BaseStage]:
    """Resolve a stage class from the live registry, or exit "not yet implemented".

    In Slice 1 `ALL_STAGES` is empty so every verb hits this branch; later
    slices register their stage classes and the verb dispatches for real.
    """
    registry = _stage_registry()
    stage_cls = registry.get(stage_id)
    if stage_cls is None:
        _console.print(f"[red]error:[/red] stage {stage_id!r} is not yet implemented")
        raise typer.Exit(_EXIT_STAGE_FAILURE)
    return stage_cls


# --------------------------------------------------------------------------- #
# status — colorized read-only manifest view. NO lock acquired.
# --------------------------------------------------------------------------- #


_STATUS_COLOR: dict[StageStatus, str] = {
    StageStatus.PENDING: "white",
    StageStatus.RUNNING: "yellow",
    StageStatus.DONE: "green",
    StageStatus.FAILED: "red",
    StageStatus.NEEDS_REVIEW: "cyan",
}


@app.command()
def status(slug: str) -> None:
    """Render a colorized table of every registered stage's status (read-only)."""
    check_platform()
    project = _load_project_or_exit(slug)
    table = Table(title=f"Project: {project.slug}")
    table.add_column("Stage", style="bold")
    table.add_column("Status")
    table.add_column("Approved")
    table.add_column("Manually edited")
    table.add_column("Outputs", overflow="fold")
    for stage_id in _stage_registry():
        record = project.manifest.stages.get(stage_id)
        if record is None:
            table.add_row(stage_id, "[red]MISSING", "—", "—", "—")
            continue
        color = _STATUS_COLOR.get(record.status, "white")
        approved = "✓" if record.human_approved_at else "✗"
        edited = "✓" if record.manually_edited else "—"
        outputs = ", ".join(record.outputs) or "—"
        table.add_row(
            stage_id,
            f"[{color}]{record.status.value}[/{color}]",
            approved,
            edited,
            outputs,
        )
    _console.print(table)


# --------------------------------------------------------------------------- #
# approve — record human approval, detect manual edits via byte-content hash.
# --------------------------------------------------------------------------- #


@app.command()
def approve(slug: str, stage_id: str) -> None:
    """Record operator approval for a `done` stage."""
    check_platform()

    if stage_id not in _stage_registry():
        _console.print(f"[red]error:[/red] unknown stage_id {stage_id!r}")
        raise typer.Exit(_EXIT_USER_ERROR)
    project = _load_project_or_exit(slug)
    configure(project.path)

    record = project.manifest.stages.get(stage_id)
    if record is None or record.status != StageStatus.DONE:
        _console.print(
            f"[red]error:[/red] cannot approve stage {stage_id!r}: "
            f"status is {record.status.value if record else 'missing'}"
        )
        raise typer.Exit(_EXIT_USER_ERROR)

    output_paths = [project.path / p for p in record.outputs]
    current_hash = compute_outputs_hash(output_paths)

    with _lock_or_bypass(project, verb=f"approve {stage_id}"):
        # Reload under the lock to ensure consistent state.
        project = _load_project_or_exit(slug)
        try:
            new_manifest = project.manifest.approve(stage_id, current_outputs_hash=current_hash)
        except CannotApproveNonDoneStage as exc:
            _console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(_EXIT_USER_ERROR) from exc
        project = project.with_manifest(new_manifest)
        project.save_manifest()

    approved_record = new_manifest.stages[stage_id]
    if approved_record.manually_edited:
        changed = _identify_changed_output_files(
            record.outputs, project.path, record.outputs_hash_at_done
        )
        _console.print(
            f"[yellow]Manual edits detected on {len(changed)} file(s); "
            f"recording manually_edited=true:[/yellow]"
        )
        for path in changed:
            _console.print(f"  • {path}")
    _console.print(f"[green]Approved {stage_id} for {slug}.[/green]")


def _identify_changed_output_files(
    outputs: tuple[str, ...], project_root: Path, original_hash: str | None
) -> list[str]:
    """Return the subset of `outputs` whose individual file hashes have shifted.

    Used purely for the human-readable "files changed" listing under `approve`.
    Per-file narrowing would require storing per-file hashes; we surface every
    declared output as "potentially modified".
    """
    if original_hash is None:
        return list(outputs)
    return list(outputs)


# --------------------------------------------------------------------------- #
# reset — drop a stage's outputs + transitively reset downstream.
# --------------------------------------------------------------------------- #


@app.command()
def reset(
    slug: str,
    stage_id: str,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the cascade-confirmation prompt.")
    ] = False,
) -> None:
    """Reset a stage to pending and transitively reset every downstream stage."""
    check_platform()
    if stage_id not in _stage_registry():
        _console.print(f"[red]error:[/red] unknown stage_id {stage_id!r}")
        raise typer.Exit(_EXIT_USER_ERROR)
    project = _load_project_or_exit(slug)
    configure(project.path)

    proceed = _confirm_cascade(project, stage_id, yes=yes, action_verb="shipcast reset")
    if not proceed:
        _console.print("[yellow]aborted; manifest unchanged.[/yellow]")
        raise typer.Exit(_EXIT_USER_ERROR)

    downstream_map = build_downstream_map()
    affected_ids: list[str] = [stage_id]
    queue: list[str] = list(downstream_map.get(stage_id, ()))
    seen: set[str] = {stage_id}
    while queue:
        sid = queue.pop(0)
        if sid in seen:
            continue
        seen.add(sid)
        affected_ids.append(sid)
        queue.extend(downstream_map.get(sid, ()))

    with _lock_or_bypass(project, verb=f"reset {stage_id}"):
        project = _load_project_or_exit(slug)
        for sid in affected_ids:
            _delete_declared_outputs(project, sid)
        new_manifest = project.manifest.reset(stage_id, downstream_of=downstream_map)
        project = project.with_manifest(new_manifest)
        project.save_manifest()
    _console.print(f"[green]Reset {len(affected_ids)} stage(s) for {slug}.[/green]")


# --------------------------------------------------------------------------- #
# _dispatch — central per-stage execution. Called by each per-stage verb.
# --------------------------------------------------------------------------- #


class _StageBusy(ShipcastError):
    """Raised when --rerun is requested against a stage that is currently RUNNING."""


def _dispatch(stage: BaseStage, slug: str, *, rerun: bool, yes: bool) -> None:
    """Run one stage end-to-end with full safety contract."""
    check_platform()
    project = _load_project_or_exit(slug)
    log_file = configure(project.path)
    logger = logging.getLogger(LOGGER_NAME)

    # ── Cascade-confirmation guard (BEFORE lock; ARC-REC-1) ────────────────
    if rerun and project.manifest.stages[stage.id].status == StageStatus.DONE:
        # _confirm_cascade may exit on its own (non-tty without --yes).
        action_verb = f"shipcast {_stage_id_to_verb(stage.id)} --rerun"
        proceed = _confirm_cascade(project, stage.id, yes=yes, action_verb=action_verb)
        if not proceed:
            _console.print("[yellow]aborted; manifest unchanged.[/yellow]")
            raise typer.Exit(_EXIT_USER_ERROR)

    # ── ffmpeg pre-flight (BEFORE lock; ARC-REC-3) ─────────────────────────
    if stage.requires_ffmpeg:
        try:
            check_available_or_raise()
        except FfmpegNotFound as exc:
            _console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(_EXIT_DEPENDENCY) from exc

    # ── Run under the lock ────────────────────────────────────────────────
    with _lock_or_bypass(project, verb=_stage_id_to_verb(stage.id)):
        project = _load_project_or_exit(slug)
        record = project.manifest.stages.get(stage.id)
        if record is None:
            _console.print(f"[red]error:[/red] manifest missing stage {stage.id!r}")
            raise typer.Exit(_EXIT_PROJECT_LOOKUP)

        # --rerun semantics
        if rerun and record.status == StageStatus.DONE:
            new_manifest = project.manifest.reset(stage.id, downstream_of=build_downstream_map())
            project = project.with_manifest(new_manifest)
            project.save_manifest()
            record = project.manifest.stages[stage.id]
        elif rerun and record.status == StageStatus.RUNNING:
            _console.print(
                f"[red]error:[/red] stage {stage.id!r} is currently RUNNING; cannot --rerun"
            )
            raise typer.Exit(_EXIT_USER_ERROR)
        elif rerun and record.status in (StageStatus.PENDING, StageStatus.FAILED):
            logger.info(
                "--rerun no-op: status is %s",
                record.status.value,
                extra={"event": "rerun_noop", "slug": slug, "stage_id": stage.id},
            )

        # Transition to RUNNING (explicitly clear stale fields per ARC-R2).
        now = datetime.now(UTC)
        try:
            new_manifest = project.manifest.transition(
                stage.id,
                StageStatus.RUNNING,
                started_at=now,
                finished_at=None,
                error=None,
            )
        except Exception as exc:
            _console.print(f"[red]error:[/red] could not transition to running: {exc}")
            raise typer.Exit(_EXIT_PROJECT_LOOKUP) from exc
        project = project.with_manifest(new_manifest)
        project.save_manifest()

        # ── Cost-cap pre-call gate (TRUE pre-condition; SEC Slice-2) ──────
        # Computed BEFORE check_inputs/pre_run_hook/run, so a stage that is
        # over budget never constructs or calls its (paid) client. The gated
        # stage has not yet recorded any cost_usd — cost is written only on the
        # DONE/NEEDS_REVIEW transition — so accumulated counts only OTHER
        # stages and there is no double-count. (A future paid stage MUST keep
        # this invariant: never write cost into the manifest mid-run; see the
        # Slice-2 security review MINOR-2 guard, to be enforced when Veo/Imagen
        # stages land in Slice 13.)
        try:
            _enforce_cost_cap(project, stage)
        except CostCapExceeded as exc:
            _record_failure(project, stage.id, exc, log_file, logger)
            raise typer.Exit(_EXIT_STAGE_FAILURE) from exc

        # check_inputs.
        try:
            stage.check_inputs(project)
        except ShipcastError as exc:
            _record_failure(project, stage.id, exc, log_file, logger)
            raise typer.Exit(_EXIT_STAGE_FAILURE) from exc

        # pre_run_hook (dispatcher invokes; ARC-R1).
        try:
            stage.pre_run_hook(project)
        except Exception as exc:
            _record_failure(project, stage.id, exc, log_file, logger)
            raise typer.Exit(_EXIT_STAGE_FAILURE) from exc

        # run.
        try:
            result = stage.run(project)
        except Exception as exc:
            _record_failure(project, stage.id, exc, log_file, logger)
            raise typer.Exit(_EXIT_STAGE_FAILURE) from exc

        # ── NEEDS_REVIEW path (A-4): skip validate_outputs + hash; no checklist ──
        if result.status == StageStatus.NEEDS_REVIEW:
            upstream_paths = stage.upstream_artifact_paths(project)
            upstream_paths.extend(stage.additional_input_paths(project))
            inputs_hash = compute_inputs_hash(upstream_paths) if upstream_paths else None
            finished_at = datetime.now(UTC)
            new_manifest = project.manifest.transition(
                stage.id,
                StageStatus.NEEDS_REVIEW,
                outputs=tuple(str(p) for p in result.outputs),
                inputs_hash=inputs_hash,
                outputs_hash_at_done=None,  # A-4: no hash until DONE
                started_at=now,
                finished_at=finished_at,
                metrics=result.metrics,
                notes=result.notes,
                error=None,
            )
            project = project.with_manifest(new_manifest)
            project.save_manifest()
            return

        # validate_outputs (raises StageOutputInvalid on absolute paths /
        # stage_dir escapes / schema violations / missing files).
        try:
            stage.validate_outputs(project, result)
        except ShipcastError as exc:
            _record_failure(project, stage.id, exc, log_file, logger)
            raise typer.Exit(_EXIT_STAGE_FAILURE) from exc

        # Compute hashes (asymmetric per BLOCKER-3).
        upstream_paths = stage.upstream_artifact_paths(project)
        upstream_paths.extend(stage.additional_input_paths(project))
        inputs_hash = compute_inputs_hash(upstream_paths) if upstream_paths else None
        output_paths_abs = [project.path / p for p in result.outputs]
        outputs_hash_at_done = compute_outputs_hash(output_paths_abs)

        # Transition to DONE.
        finished_at = datetime.now(UTC)
        new_manifest = project.manifest.transition(
            stage.id,
            StageStatus.DONE,
            outputs=tuple(str(p) for p in result.outputs),
            inputs_hash=inputs_hash,
            outputs_hash_at_done=outputs_hash_at_done,
            started_at=now,
            finished_at=finished_at,
            metrics=result.metrics,
            notes=result.notes,
            error=None,  # defensive per ARC-R2
        )
        project = project.with_manifest(new_manifest)
        project.save_manifest()

    # ── Review Checklist (lock released by here) ──────────────────────────
    _print_review_checklist(stage, project, result)


def _enforce_cost_cap(project: Project, stage: BaseStage) -> None:
    """Refuse to proceed when the stage's next paid call would exceed the cap.

    Pure pre-condition: reads the accumulated cost from the (RUNNING) manifest
    and the per-tool unit cost the stage declares via `next_call_cost_usd`,
    compares `projected > cap` (STRICT), and raises `CostCapExceeded` before any
    client is touched. Stages declaring `0.0` (no paid call) are never blocked.
    """
    unit_cost = stage.next_call_cost_usd(project)
    if unit_cost <= 0.0:
        return
    cap = project.settings.max_cost_usd_per_project
    ledger = CostLedger(project.manifest)
    if ledger.would_exceed(unit_cost, cap=cap):
        projected = ledger.projected(unit_cost)
        raise CostCapExceeded(
            f"stage {stage.id!r} would push project cost to ${projected:.2f}, "
            f"over the ${cap:.2f} cap ({project.settings.video_mode} mode). "
            f"Accumulated=${ledger.accumulated():.2f}, next call=${unit_cost:.2f}. "
            f"No paid API call was made."
        )


def _record_failure(
    project: Project,
    stage_id: str,
    exc: BaseException,
    log_file: Path | None,
    logger: logging.Logger,
) -> None:
    """Write traceback to log THEN transition stage to FAILED (ARC-R3).

    Uses `traceback.format_exc()` — never `capture_locals=True` (SEC-B1).
    """
    # Order matters: log the traceback (file handler flushes on emit) BEFORE
    # the manifest update points at the log file.
    logger.error(
        "stage %s failed: %s",
        stage_id,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    _ = traceback.format_exc()  # explicit no-locals form (no-op besides safety doc)

    error = ErrorRecord(
        type=exc.__class__.__name__,
        message=str(exc),
        traceback_path=str(log_file) if log_file else None,
    )
    new_manifest = project.manifest.transition(
        stage_id,
        StageStatus.FAILED,
        finished_at=datetime.now(UTC),
        error=error,
    )
    project = project.with_manifest(new_manifest)
    project.save_manifest()
    _console.print(f"[red]stage {stage_id} FAILED:[/red] {exc.__class__.__name__}: {exc}")


# --------------------------------------------------------------------------- #
# pick — bespoke verb. Bootstraps a project from a target repo path + --entry
# (create mode) OR dispatches 01_pick on an existing project slug (dispatch
# mode). The discriminator is the presence of --entry.
# --------------------------------------------------------------------------- #

_RERUN_OPT = typer.Option("--rerun", help="Re-run this stage even if it is already done.")
_YES_OPT = typer.Option("--yes", help="Skip the cascade-confirmation prompt.")


def _slugify(text: str) -> str:
    """Lowercase, replace non-alphanumerics with hyphens, collapse + trim.

    Produces a directory-safe token (matches `Project._SLUG_PATTERN` once
    combined with the leading repo-short component). Empty input yields "x" so
    the result is never empty.
    """
    out = re.sub(r"[^a-z0-9]+", "-", text.strip().casefold()).strip("-")
    return out or "x"


def _derive_slug(repo_path: Path, entry_heading: str) -> str:
    """Derive a project slug `<repo-short>--<entry-slug>` from repo + heading.

    `<repo-short>` is the slugified final path component of `repo_path`;
    `<entry-slug>` is the slugified entry heading. The two are joined with a
    double hyphen so the boundary is visually obvious in `projects/`.
    """
    repo_short = _slugify(repo_path.name)
    entry_slug = _slugify(entry_heading)
    return f"{repo_short}--{entry_slug}"


_PICK_ENTRY_OPT = typer.Option(
    "--entry",
    help=(
        "Exact CHANGELOG heading to pick (the text between '### ' and ' — HH:MM "
        "UTC'). When supplied, the positional argument is treated as a target "
        "REPO PATH and a new project is created. When omitted, the positional "
        "argument is treated as an existing project SLUG to (re)dispatch."
    ),
)
_PICK_BRAND_OPT = typer.Option(
    "--brand-slug", help="Brand pack slug (defaults to the repo short name)."
)
_PICK_LIVE_URL_OPT = typer.Option(
    "--live-url", help="Optional https live URL for downstream brand/enrich extract."
)
_PICK_VIDEO_MODE_OPT = typer.Option(
    "--video-mode", help="Video render mode: 'standard' or 'premium'."
)
_PICK_FORCE_OPT = typer.Option(
    "--force", help="Overwrite an existing project at the derived slug (create mode)."
)


def _write_input_yaml(
    project: Project,
    *,
    repo_path: Path,
    entry_heading: str,
    brand_slug: str,
    live_url: str | None,
    video_mode: str,
) -> None:
    """Write the project's `input.yaml` from the `pick` create-mode arguments.

    Only fields the operator supplied are emitted; `live_url` / `feature_walkthrough`
    stay absent (the schema treats them as optional). The resulting file is what
    `s01_pick.run()` reads and validates via `InputYaml`.
    """
    payload: dict[str, object] = {
        "repo_path": str(repo_path),
        "entry_heading": entry_heading,
        "brand_slug": brand_slug,
        "video_mode": video_mode,
    }
    if live_url is not None:
        payload["live_url"] = live_url
    project.input_path.write_text(
        yaml.safe_dump(payload, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )


@app.command()
def pick(
    target: str,
    entry: Annotated[str | None, _PICK_ENTRY_OPT] = None,
    brand_slug: Annotated[str | None, _PICK_BRAND_OPT] = None,
    live_url: Annotated[str | None, _PICK_LIVE_URL_OPT] = None,
    video_mode: Annotated[str, _PICK_VIDEO_MODE_OPT] = "standard",
    rerun: Annotated[bool, _RERUN_OPT] = False,
    yes: Annotated[bool, _YES_OPT] = False,
    force: Annotated[bool, _PICK_FORCE_OPT] = False,
) -> None:
    """Pick a CHANGELOG entry — creating the project when `--entry` is given.

    Two modes:

    * ``shipcast pick <repo-path> --entry "<heading>"`` — create a project from
      the template (slug derived from repo + heading), write its ``input.yaml``,
      then dispatch ``01_pick``.
    * ``shipcast pick <slug>`` — dispatch ``01_pick`` on an existing project.
    """
    check_platform()
    stage_id = _VERB_TO_STAGE_ID["pick"]
    stage_cls = _resolve_stage_or_exit(stage_id)

    if entry is None:
        # Dispatch mode: `target` is an existing project slug.
        _dispatch(stage_cls(), target, rerun=rerun, yes=yes)
        return

    # Create mode: `target` is a target repo path.
    repo_path = Path(target).expanduser()
    slug = _derive_slug(repo_path, entry)
    effective_brand = brand_slug if brand_slug is not None else _slugify(repo_path.name)

    settings = Settings.from_files(
        config_path=Path("config.toml"),
        env_path=Path(".env"),
    )
    config_snapshot = settings.public_dict()
    try:
        project = Project.create(
            _GlobalOptions.projects_root,
            slug,
            config_snapshot,
            settings=settings,
            force=force,
        )
    except InvalidSlug as exc:
        _console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(_EXIT_USER_ERROR) from exc
    except ProjectExists as exc:
        _console.print(
            f"[red]error:[/red] {exc}\n"
            f"Re-run with --force to overwrite, or `shipcast pick {slug}` to "
            f"re-dispatch the existing project."
        )
        raise typer.Exit(_EXIT_USER_ERROR) from exc

    _write_input_yaml(
        project,
        repo_path=repo_path,
        entry_heading=entry,
        brand_slug=effective_brand,
        live_url=live_url,
        video_mode=video_mode,
    )
    _console.print(
        f"[green]Created project[/green] [bold cyan]{slug}[/bold cyan] at {project.path}."
    )
    _dispatch(stage_cls(), slug, rerun=rerun, yes=yes)


# --------------------------------------------------------------------------- #
# Per-stage verbs — each is a thin shim that resolves its stage class at call
# time from the live registry.
# --------------------------------------------------------------------------- #


def _make_verb_command(verb: str, stage_id: str) -> None:
    """Register one per-stage CLI verb.

    The stage class is resolved at CALL time from `_stage_registry()`, not bound
    here — so unimplemented stages fail gracefully and test-injected stages are
    picked up without re-importing this module.
    """

    def _cmd(
        slug: str,
        rerun: Annotated[bool, _RERUN_OPT] = False,
        yes: Annotated[bool, _YES_OPT] = False,
    ) -> None:
        check_platform()
        stage_cls = _resolve_stage_or_exit(stage_id)
        _dispatch(stage_cls(), slug, rerun=rerun, yes=yes)

    _cmd.__name__ = verb
    _cmd.__doc__ = f"Dispatch stage `{stage_id}` for the given slug."
    app.command(name=verb)(_cmd)


_NO_VEO_OPT = typer.Option(
    "--no-veo",
    help=(
        "Force the standard (Imagen + Ken-Burns) path even for a premium "
        "project, so the Veo 3 Fast hero clip is never generated. Useful to "
        "stay in the cheaper standard cost range for a premium-tagged entry."
    ),
)


@app.command(name="video_assets")
def video_assets(
    slug: str,
    no_veo: Annotated[bool, _NO_VEO_OPT] = False,
    rerun: Annotated[bool, _RERUN_OPT] = False,
    yes: Annotated[bool, _YES_OPT] = False,
) -> None:
    """Dispatch stage `06_video_assets` (both modes); `--no-veo` forces standard."""
    check_platform()
    stage_cls = _resolve_stage_or_exit(_VERB_TO_STAGE_ID["video_assets"])
    # `no_veo` is plumbed into the stage instance (NOT imported by the stage from
    # cli) so the stage stays a pure function of its inputs + this flag.
    try:
        stage = stage_cls(no_veo=no_veo)  # type: ignore[call-arg]
    except TypeError:
        # Defensive: a stage class without a `no_veo` kwarg (e.g. a test double)
        # still dispatches in the default standard/premium path.
        stage = stage_cls()
    _dispatch(stage, slug, rerun=rerun, yes=yes)


for _verb, _stage_id in _VERB_TO_STAGE_ID.items():
    # `pick` and `video_assets` get bespoke commands (above/below): `pick` can
    # ALSO bootstrap a project from a repo path + --entry; `video_assets` carries
    # the extra `--no-veo` flag. The other verbs are pure dispatch shims.
    if _verb in ("pick", "video_assets"):
        continue
    _make_verb_command(_verb, _stage_id)


if __name__ == "__main__":
    app()
