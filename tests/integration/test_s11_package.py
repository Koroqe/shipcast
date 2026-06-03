"""Integration tests for the `s11_package` stage (Slice 20).

Owned TCs (Section 14 + Section 21):
- TC-14.1: happy path — ``release.zip`` listing contains all required upstream
           assets (showcase.mp4, loop_6s.mp4, loop_6s.gif, the 4 aspect cards,
           og_card.png, the 6 carousel slides, the 3 markdown files); ``README.md``
           has >= 3 fenced code blocks (one per copy channel) and a >= 9-row
           asset table with dimensions + aspect for every asset.
- TC-14.2: conditional ``stat_1x1.png`` + ``code.png`` present in ``09_graphics``
           are bundled into the ZIP (and appear in the README table).
- TC-14.3: those conditional files absent → not in the ZIP listing.
- TC-14.4 / TC-21.2: re-running on identical inputs → byte-identical ``release.zip``.
- TC-14.5: the optional ``code-reviewer`` sub-agent timing out → SubagentTimeout
           (stage fails, no ZIP / README left behind).

The optional ``code-reviewer`` `claude -p` call is mocked through the stage's
injected ``subprocess_run``; no real ``claude`` / network. The upstream stages
(08/09/10) are NOT actually run — the project's manifest is seeded directly with
their declared outputs + fixture files on disk, mirroring ``test_s08_video.py``.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from shipcast.config import Settings
from shipcast.manifest import Manifest, StageStatus
from shipcast.project import Project
from shipcast.stages.s11_package import PackageStage

# A minimal but valid 1x1 PNG (header + IHDR + IDAT + IEND).
_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

# The always-present 09_graphics outputs (aspect cards + OG + 6 carousel slides).
_GRAPHICS_BASE: tuple[str, ...] = (
    "09_graphics/1x1.png",
    "09_graphics/16x9.png",
    "09_graphics/9x16.png",
    "09_graphics/4x5.png",
    "09_graphics/og_card.png",
    "09_graphics/carousel/slide_01.png",
    "09_graphics/carousel/slide_02.png",
    "09_graphics/carousel/slide_03.png",
    "09_graphics/carousel/slide_04.png",
    "09_graphics/carousel/slide_05.png",
    "09_graphics/carousel/slide_06.png",
)

_VIDEO_OUTPUTS: tuple[str, ...] = (
    "08_video/showcase.mp4",
    "08_video/loop_6s.mp4",
    "08_video/loop_6s.gif",
)

_COPY_OUTPUTS: tuple[str, ...] = (
    "10_copy/twitter_thread.md",
    "10_copy/linkedin.md",
    "10_copy/blog.md",
)

_TWITTER_MD = "1/ we just shipped CSV export.\n2/ try it today.\n"
_LINKEDIN_MD = "Before, exports were manual.\n\n→ now one click.\n"
_BLOG_MD = "# CSV export\n\nThe full story of how we shipped it.\n"

_COPY_BODIES = {
    "10_copy/twitter_thread.md": _TWITTER_MD,
    "10_copy/linkedin.md": _LINKEDIN_MD,
    "10_copy/blog.md": _BLOG_MD,
}


def _write_file(project: Project, rel: str, *, png: bool = False) -> None:
    full = project.path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    if rel in _COPY_BODIES:
        full.write_text(_COPY_BODIES[rel], encoding="utf-8")
    elif png:
        full.write_bytes(_PNG_1x1)
    else:
        # mp4 / gif — opaque bytes; dimensions come from constants, not the file.
        full.write_bytes(b"\x00\x01\x02\x03binary-asset")


def _build_project(
    tmp_path: Path,
    *,
    with_stat: bool = False,
    with_code: bool = False,
) -> Project:
    """Seed a project with 08/09/10 done+approved and their outputs on disk."""
    project = Project.create(
        tmp_path,
        "entry",
        {},
        settings=Settings(),
    )

    graphics_outputs = list(_GRAPHICS_BASE)
    if with_stat:
        graphics_outputs.append("09_graphics/stat_1x1.png")
    if with_code:
        graphics_outputs.append("09_graphics/code.png")

    # Write the on-disk artifact files.
    for rel in _VIDEO_OUTPUTS:
        _write_file(project, rel, png=False)
    for rel in graphics_outputs:
        _write_file(project, rel, png=True)
    for rel in _COPY_OUTPUTS:
        _write_file(project, rel, png=False)

    m = Manifest.load(project.manifest_path)
    for sid, outs in (
        ("08_video", _VIDEO_OUTPUTS),
        ("09_graphics", tuple(graphics_outputs)),
        ("10_copy", _COPY_OUTPUTS),
    ):
        m = m.transition(sid, StageStatus.RUNNING)
        m = m.transition(sid, StageStatus.DONE, outputs=outs)
        m = m.approve(sid)
    m.save(project.manifest_path)
    return Project.load(tmp_path, "entry")


def _ok_subprocess() -> MagicMock:
    """A `claude -p` mock returning a clean code-reviewer pass."""
    calls = MagicMock()

    def _run(cmd: list[str], *a: Any, **k: Any) -> Any:
        calls(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="LGTM", stderr="")

    calls.run = _run
    return calls


def _stage(calls: MagicMock | None = None) -> PackageStage:
    if calls is None:
        calls = _ok_subprocess()
    return PackageStage(subprocess_run=calls.run)


def _zip_names(project: Project) -> set[str]:
    zip_path = project.stage_dir("11_package") / "release.zip"
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


def _count_fenced_blocks(readme: str) -> int:
    # Count opening fences only (every block has a matching close fence).
    fences = [ln for ln in readme.splitlines() if ln.startswith("```")]
    return len(fences) // 2


def _count_table_rows(readme: str) -> int:
    # Markdown table data rows: `| ... |` lines that are not the header/separator.
    rows = 0
    for ln in readme.splitlines():
        s = ln.strip()
        if s.startswith("|") and s.endswith("|") and set(s) - set("| -:"):
            rows += 1
    # Subtract the header row (first non-separator pipe row).
    return max(0, rows - 1)


# --------------------------------------------------------------------------- #
# TC-14.1 — happy path
# --------------------------------------------------------------------------- #


def test_tc_14_1_zip_has_all_assets_and_readme_blocks_and_table(
    tmp_path: Path,
) -> None:
    project = _build_project(tmp_path)
    result = _stage().run(project)

    assert result.status == StageStatus.DONE
    pkg_dir = project.stage_dir("11_package")
    assert (pkg_dir / "release.zip").is_file()
    assert (pkg_dir / "README.md").is_file()

    names = _zip_names(project)
    for rel in (*_VIDEO_OUTPUTS, *_GRAPHICS_BASE, *_COPY_OUTPUTS):
        assert rel in names, f"missing {rel} from ZIP listing"

    readme = (pkg_dir / "README.md").read_text(encoding="utf-8")
    assert _count_fenced_blocks(readme) >= 3, readme
    assert _count_table_rows(readme) >= 9, readme

    # The paste-ready copy bodies are embedded verbatim in the README.
    assert _TWITTER_MD.strip() in readme
    assert _LINKEDIN_MD.strip() in readme
    assert _BLOG_MD.strip() in readme

    # Every bundled asset appears as a README table row with its arcname.
    for rel in (*_VIDEO_OUTPUTS, *_GRAPHICS_BASE, *_COPY_OUTPUTS):
        assert rel in readme, f"{rel} missing from README table"

    # The StageResult declares the two artifacts.
    assert Path("11_package/release.zip") in result.outputs
    assert Path("11_package/README.md") in result.outputs


def test_tc_14_1_readme_table_has_dimensions_and_aspect(tmp_path: Path) -> None:
    project = _build_project(tmp_path)
    _stage().run(project)
    readme = (project.stage_dir("11_package") / "README.md").read_text(
        encoding="utf-8"
    )
    # A 16:9 card row shows its pixel dimensions and aspect token.
    assert "1920x1080" in readme
    assert "16:9" in readme
    # The showcase video row shows its vertical dimensions.
    assert "1080x1920" in readme


# --------------------------------------------------------------------------- #
# TC-14.2 — conditional stat + code included when present
# --------------------------------------------------------------------------- #


def test_tc_14_2_conditional_files_included_when_present(tmp_path: Path) -> None:
    project = _build_project(tmp_path, with_stat=True, with_code=True)
    _stage().run(project)
    names = _zip_names(project)
    assert "09_graphics/stat_1x1.png" in names
    assert "09_graphics/code.png" in names
    readme = (project.stage_dir("11_package") / "README.md").read_text(
        encoding="utf-8"
    )
    assert "09_graphics/stat_1x1.png" in readme
    assert "09_graphics/code.png" in readme


# --------------------------------------------------------------------------- #
# TC-14.3 — conditional files absent when not produced
# --------------------------------------------------------------------------- #


def test_tc_14_3_conditional_files_absent_when_not_produced(tmp_path: Path) -> None:
    project = _build_project(tmp_path, with_stat=False, with_code=False)
    _stage().run(project)
    names = _zip_names(project)
    assert "09_graphics/stat_1x1.png" not in names
    assert "09_graphics/code.png" not in names


# --------------------------------------------------------------------------- #
# TC-14.4 / TC-21.2 — byte-identical ZIP on re-run
# --------------------------------------------------------------------------- #


def test_tc_14_4_zip_byte_identical_on_rerun(tmp_path: Path) -> None:
    project_a = _build_project(tmp_path / "a")
    _stage().run(project_a)
    bytes_a = (project_a.stage_dir("11_package") / "release.zip").read_bytes()

    project_b = _build_project(tmp_path / "b")
    _stage().run(project_b)
    bytes_b = (project_b.stage_dir("11_package") / "release.zip").read_bytes()

    assert bytes_a == bytes_b


def test_tc_21_2_zip_byte_identical_same_project_rerun(tmp_path: Path) -> None:
    project = _build_project(tmp_path)
    _stage().run(project)
    first = (project.stage_dir("11_package") / "release.zip").read_bytes()
    # Re-run in place (same inputs) — must reproduce identical bytes.
    _stage().run(project)
    second = (project.stage_dir("11_package") / "release.zip").read_bytes()
    assert first == second


# --------------------------------------------------------------------------- #
# TC-14.5 — code-reviewer timeout → SubagentTimeout, no artifacts
# --------------------------------------------------------------------------- #


def test_tc_14_5_code_reviewer_timeout_fails(tmp_path: Path) -> None:
    from shipcast.errors import SubagentTimeout

    project = _build_project(tmp_path)

    def _timeout_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, timeout=300)

    stage = PackageStage(subprocess_run=_timeout_run)
    with pytest.raises(SubagentTimeout):
        stage.run(project)

    # No partial artifacts left on disk.
    pkg_dir = project.stage_dir("11_package")
    assert not (pkg_dir / "release.zip").exists()
    assert not (pkg_dir / "README.md").exists()


def test_code_reviewer_invoked_with_claude_agent(tmp_path: Path) -> None:
    project = _build_project(tmp_path)
    calls = _ok_subprocess()
    _stage(calls).run(project)
    # The optional code-reviewer claude -p call was made.
    assert calls.call_count == 1
    cmd = calls.call_args[0][0]
    assert cmd[0] == "claude"
    assert "code-reviewer" in cmd
