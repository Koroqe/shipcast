"""Centralized path resolution.

Maps stage ids (from the manifest) to their on-disk subdirectory under
`projects/<slug>/`. For shipcast the mapping is the identity — the stage_id
*is* the directory name (e.g. `"01_pick"` → `01_pick/`). The mapping is kept
explicit (rather than derived) so the canonical stage order and directory
contract live in exactly one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: stage_id (manifest key) → on-disk subdirectory name under projects/<slug>/.
#: For shipcast this is the identity mapping: stage_id == dir name.
STAGE_DIR_NAMES: Final[dict[str, str]] = {
    "01_pick": "01_pick",
    "02_enrich": "02_enrich",
    "03_brand": "03_brand",
    "04_plan": "04_plan",
    "05_script": "05_script",
    "06_video_assets": "06_video_assets",
    "07_voice": "07_voice",
    "08_video": "08_video",
    "09_graphics": "09_graphics",
    "10_copy": "10_copy",
    "11_package": "11_package",
}

#: Canonical stage order. Useful for status tables and template materialization.
STAGE_IDS: Final[tuple[str, ...]] = tuple(STAGE_DIR_NAMES.keys())


def default_template_path() -> Path:
    """Locate `projects/_template/` relative to this source file.

    Works for `uv run`-style execution from the repo. When the package is
    installed via wheel, callers should pass an explicit `template_path` to
    `Project.create` instead — packaging the template into the wheel will be
    addressed in a later slice.
    """
    shipcast_dir = Path(__file__).resolve().parent
    repo_root = shipcast_dir.parent.parent
    return repo_root / "projects" / "_template"


def default_prompts_path() -> Path:
    """Locate the repo's `prompts/` directory relative to this source file.

    Parallel convention to `default_template_path`. Stage modules that need
    to render a Jinja2 template (currently only `shipcast.prompts.render_prompt`)
    use this as the `FileSystemLoader` search path. Packaging the prompts
    into the wheel via `importlib.resources` will be addressed when stage
    implementations ship in installable form.
    """
    shipcast_dir = Path(__file__).resolve().parent
    repo_root = shipcast_dir.parent.parent
    return repo_root / "prompts"
