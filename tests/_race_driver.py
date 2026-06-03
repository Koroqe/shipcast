"""Subprocess entry point for the two-process lock-contention race (TC-18.1).

NOT a test module — invoked via `python -m tests._race_driver <projects_root> <slug>`
from `test_locking.py`. Each process:

1. Registers a `FakeStage` (id `01_pick`) whose `pre_run_hook` sleeps, widening
   the window during which the project lock is held (so the second process is
   guaranteed to collide). The sleep is injected via `pre_run_hook` — the
   production code NEVER branches on a test-only env var (testing rule).
2. Dispatches `shipcast pick <slug>` against the shared project.

Exit code is whatever the CLI returns: 0 on success, 4 (_EXIT_CONCURRENCY) when
the lock is already held (ProjectLocked). The parent test asserts exactly one
0 and one 4 across the two processes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

import shipcast.stages as _stages
from shipcast.project import Project
from shipcast.stage import StageResult, StageStatus
from shipcast.stages import BaseStage


class _PickOut(BaseModel):
    ok: bool


class FakeStage(BaseStage):
    """Minimal stage that holds the lock long enough to force contention."""

    id: ClassVar[str] = "01_pick"
    requires: ClassVar[tuple[str, ...]] = ()
    output_schema: ClassVar[type[BaseModel]] = _PickOut
    review_checklist_items: ClassVar[tuple[str, ...]] = ("check the picked entry",)

    def pre_run_hook(self, project: Project) -> None:
        # Widen the locked window. Injected here — never via an env-var branch
        # inside production code.
        time.sleep(1.5)

    def run(self, project: Project) -> StageResult:
        out_dir = project.stage_dir(self.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "entry.json"
        out_file.write_text('{\n  "ok": true\n}\n', encoding="utf-8")
        rel = out_file.relative_to(project.path)
        return StageResult(status=StageStatus.DONE, outputs=(rel,))


def main() -> int:
    projects_root = Path(sys.argv[1])
    slug = sys.argv[2]

    # Inject the fake stage into the live registry the dispatcher reads.
    _stages.ALL_STAGES = (FakeStage,)

    from typer.testing import CliRunner

    from shipcast.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--projects-root", str(projects_root), "pick", slug])
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
