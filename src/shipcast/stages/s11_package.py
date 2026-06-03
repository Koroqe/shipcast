"""Stage 11 — package (``release.zip`` + paste-ready ``README.md``).

Bundles every marketing asset the pipeline produced (stages 08/09/10) into a
single deterministic ``11_package/release.zip`` and writes a human-facing
``11_package/README.md`` that an operator can copy-paste from:

* one fenced code block per copy channel (X thread / LinkedIn / blog), the raw
  text ready to paste into each platform (FR-13.2);
* a Markdown asset TABLE — one row per bundled file, with its pixel dimensions
  and aspect ratio (FR-13.2).

Asset set (FR-13.1 / FR-13.4)
-----------------------------
The bundled files are read from the DECLARED ``outputs`` of the three immediate
upstream stages in the manifest (``08_video`` / ``09_graphics`` / ``10_copy``),
NOT from a hard-coded list. This is what makes the conditional graphics
(``stat_*.png`` when ``brief.has_stat_card``; ``code.png`` when
``brief.has_code_screenshot``) flow through automatically: they appear in
``09_graphics``'s outputs only when that stage actually rendered them, so they
land in the ZIP only when present (TC-14.2 / TC-14.3).

Determinism (FR-13.3 / TC-14.4 / TC-21.2)
-----------------------------------------
``release.zip`` is byte-identical across re-runs on identical inputs:

* members are written in SORTED ``arcname`` order;
* every ``ZipInfo`` uses a FIXED ``date_time`` (the ZIP epoch ``1980-01-01``)
  and fixed external attrs, so the per-run wall clock never leaks into the
  archive;
* a fixed DEFLATE compression level is used (zlib deflate is deterministic for
  identical input bytes at a fixed level).

The README is likewise rendered from the SORTED asset list, so it is stable too.

Optional ``code-reviewer`` sanity check (FR-13.5 / TC-14.5)
-----------------------------------------------------------
After writing the README, the stage runs the existing ``code-reviewer``
sub-agent via ``claude -p`` (300 s budget) to sanity-check the README's
paste-blocks/links. A timeout raises :class:`SubagentTimeout` and a non-zero
exit raises :class:`SubagentFailed`; either way the stage fails and the
just-written ``release.zip`` / ``README.md`` are removed so no partial artifacts
are left behind. The call authenticates via the operator's local ``claude``
subscription and incurs NO per-call USD cost.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from shipcast.errors import (
    StageInputMissing,
    StageOutputInvalid,
    SubagentFailed,
    SubagentTimeout,
)
from shipcast.manifest import StageStatus
from shipcast.schemas import AssetEntry, PackageManifest
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from collections.abc import Callable

    from shipcast.project import Project

#: Wall-clock budget for the optional code-reviewer sub-agent invocation.
_SUBAGENT_TIMEOUT_SEC: int = 300

#: Fixed ZIP member timestamp (DOS epoch) so per-run wall-clock never leaks into
#: the archive — required for byte-identical re-runs (TC-14.4 / TC-21.2).
_ZIP_EPOCH: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)

#: Fixed DEFLATE level (deterministic for identical input bytes).
_ZIP_COMPRESSLEVEL: int = 6

#: Known fixed pixel dimensions by output filename. Images would also be
#: readable via PIL, but video / GIF dimensions are pipeline constants, and
#: pinning the card dims here keeps the README table render free of PIL I/O for
#: the deterministic asset set. Filenames not listed fall back to PIL (images)
#: or the text sentinel.
_DIMS_BY_NAME: dict[str, tuple[int, int]] = {
    # 08_video
    "showcase.mp4": (1080, 1920),
    "loop_6s.mp4": (1080, 1080),
    "loop_6s.gif": (1080, 1080),
    # 09_graphics aspect cards
    "1x1.png": (1080, 1080),
    "16x9.png": (1920, 1080),
    "9x16.png": (1080, 1920),
    "4x5.png": (1080, 1350),
    "og_card.png": (1200, 630),
    # 09_graphics conditional stat cards
    "stat_1x1.png": (1080, 1080),
    "stat_16x9.png": (1920, 1080),
    "stat_9x16.png": (1080, 1920),
    "stat_4x5.png": (1080, 1350),
}

#: Channel field -> (output relpath, README heading) in write order.
_CHANNELS: tuple[tuple[str, str, str], ...] = (
    ("x", "10_copy/twitter_thread.md", "X thread"),
    ("linkedin", "10_copy/linkedin.md", "LinkedIn"),
    ("blog", "10_copy/blog.md", "Blog"),
)

#: Text-asset sentinel for the dimensions / aspect columns.
_TEXT_SENTINEL: str = "—"


def _aspect_token(width: int, height: int) -> str:
    """Reduce ``width:height`` to a small aspect token (e.g. ``16:9``)."""
    from math import gcd

    g = gcd(width, height) or 1
    return f"{width // g}:{height // g}"


class PackageStage(BaseStage):
    """Produce ``11_package/{release.zip,README.md}`` from stages 08/09/10."""

    id: ClassVar[str] = "11_package"
    requires: ClassVar[tuple[str, ...]] = ("08_video", "09_graphics", "10_copy")
    output_schema: ClassVar[type[PackageManifest]] = PackageManifest
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Unzip release.zip and confirm it contains the showcase video, the "
        "6-second loop + GIF, all four aspect cards, the OG card, the six "
        "carousel slides, and the three copy markdown files.",
        "Open README.md and paste-test each channel block (X / LinkedIn / blog) "
        "into its platform — confirm formatting survives the paste.",
        "Scan the asset table — every bundled file has plausible dimensions and "
        "aspect ratio, and the conditional stat / code graphics appear only if "
        "the brief asked for them.",
    )

    ZIP_REL: ClassVar[str] = "release.zip"
    README_REL: ClassVar[str] = "README.md"

    def __init__(
        self,
        *,
        subprocess_run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        # Indirection so tests inject a fake `claude -p` without patching the
        # global `subprocess` module. Defaults to the real `subprocess.run`.
        self._subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = (
            subprocess_run or subprocess.run
        )

    # ------------------------------------------------------------- asset set
    def _bundled_relpaths(self, project: Project) -> list[str]:
        """Collect every upstream output relpath to bundle, sorted + de-duped.

        Reads the DECLARED ``outputs`` of the three immediate upstream stages so
        conditional graphics flow through automatically (FR-13.4). Raises
        :class:`StageInputMissing` if a declared upstream output is absent on
        disk (defense-in-depth; ``check_inputs`` already verifies this).
        """
        rels: set[str] = set()
        for upstream_id in self.requires:
            record = project.manifest.stages.get(upstream_id)
            if record is None:
                continue
            for rel in record.outputs:
                full = project.path / rel
                if not full.is_file():
                    raise StageInputMissing(
                        f"stage {self.id!r} cannot bundle {rel!r}: file missing "
                        f"at {full}"
                    )
                rels.add(rel)
        return sorted(rels)

    # ------------------------------------------------------------- dimensions
    def _dimensions(self, project: Project, rel: str) -> tuple[str, str]:
        """Return ``(dimensions, aspect)`` strings for one bundled asset.

        Markdown (``.md``) assets are text → both columns are the sentinel.
        Known image/video filenames use the pinned constant dims; any other
        image is measured with PIL.
        """
        name = Path(rel).name
        if name.endswith(".md"):
            return _TEXT_SENTINEL, _TEXT_SENTINEL
        dims = _DIMS_BY_NAME.get(name)
        if dims is None and name.lower().endswith((".png", ".jpg", ".jpeg")):
            from PIL import Image

            with Image.open(project.path / rel) as img:
                dims = (img.width, img.height)
        if dims is None:
            return _TEXT_SENTINEL, _TEXT_SENTINEL
        width, height = dims
        return f"{width}x{height}", _aspect_token(width, height)

    # ------------------------------------------------------------- manifest
    def _build_manifest(self, project: Project, rels: list[str]) -> PackageManifest:
        """Assemble the in-memory :class:`PackageManifest` (sorted assets)."""
        assets: list[AssetEntry] = []
        for rel in rels:
            dims, aspect = self._dimensions(project, rel)
            assets.append(AssetEntry(arcname=rel, dimensions=dims, aspect=aspect))
        channels = [channel for channel, _rel, _heading in _CHANNELS]
        return PackageManifest(assets=assets, channels=channels)

    # ------------------------------------------------------------- README
    def _render_readme(self, project: Project, pkg: PackageManifest) -> str:
        """Render the paste-ready README: per-channel fenced blocks + asset table."""
        lines: list[str] = [
            f"# Release package — {project.slug}",
            "",
            "Everything you need to ship this entry's marketing. The "
            "channel blocks below are paste-ready; `release.zip` bundles every "
            "asset listed in the table.",
            "",
            "## Paste-ready copy",
            "",
        ]
        for _channel, rel, heading in _CHANNELS:
            body = (project.path / rel).read_text(encoding="utf-8").rstrip("\n")
            lines.append(f"### {heading}")
            lines.append("")
            lines.append("```text")
            lines.append(body)
            lines.append("```")
            lines.append("")

        lines.append("## Assets")
        lines.append("")
        lines.append("| Asset | Dimensions | Aspect |")
        lines.append("| --- | --- | --- |")
        for asset in pkg.assets:
            lines.append(
                f"| `{asset.arcname}` | {asset.dimensions} | {asset.aspect} |"
            )
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------- zip
    def _write_zip(self, project: Project, zip_path: Path, rels: list[str]) -> None:
        """Write ``release.zip`` deterministically (sorted, fixed timestamps)."""
        with zipfile.ZipFile(
            zip_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=_ZIP_COMPRESSLEVEL,
        ) as zf:
            for rel in rels:  # already sorted by _bundled_relpaths
                info = zipfile.ZipInfo(filename=rel, date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16  # fixed unix mode
                data = (project.path / rel).read_bytes()
                zf.writestr(info, data)

    # ------------------------------------------------------------- sub-agent
    def _review_readme(self, readme_path: Path) -> None:
        """Run the optional ``code-reviewer`` sub-agent on the README (FR-13.5).

        Raises:
            SubagentTimeout: the subprocess exceeded the 300 s budget.
            SubagentFailed: the subprocess exited non-zero (stderr captured).
        """
        prompt = (
            "Sanity-check this release README for broken paste-blocks or links. "
            "Report any obvious problems.\n\n"
            f"{readme_path.read_text(encoding='utf-8')}"
        )
        try:
            result = self._subprocess_run(
                [
                    "claude",
                    "-p",
                    "--agent",
                    "code-reviewer",
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
                f"code-reviewer exceeded {_SUBAGENT_TIMEOUT_SEC}s timeout"
            ) from exc
        if result.returncode != 0:
            raise SubagentFailed(
                "code-reviewer", result.returncode, result.stderr or ""
            )

    # ------------------------------------------------------------- run
    def run(self, project: Project) -> StageResult:
        """Assemble ``release.zip`` + ``README.md`` and run the README sanity check."""
        rels = self._bundled_relpaths(project)
        pkg = self._build_manifest(project, rels)

        stage_dir = project.stage_dir(self.id)
        stage_dir.mkdir(parents=True, exist_ok=True)
        zip_path = stage_dir / self.ZIP_REL
        readme_path = stage_dir / self.README_REL

        self._write_zip(project, zip_path, rels)
        readme_path.write_text(self._render_readme(project, pkg), encoding="utf-8")

        # Optional code-reviewer sanity check. On failure, remove the partial
        # artifacts so the FAILED stage leaves nothing behind (TC-14.5).
        try:
            self._review_readme(readme_path)
        except (SubagentTimeout, SubagentFailed):
            zip_path.unlink(missing_ok=True)
            readme_path.unlink(missing_ok=True)
            raise

        return StageResult(
            status=StageStatus.DONE,
            outputs=(
                Path(self.id) / self.ZIP_REL,
                Path(self.id) / self.README_REL,
            ),
            metrics={"cost_usd": 0.0},
        )

    # ------------------------------------------------------------- validate
    def validate_outputs(self, project: Project, result: StageResult) -> None:
        """Confirm both artifacts exist and the ZIP is a readable archive.

        The default single-schema ``validate_outputs`` does not apply: this
        stage's outputs are a binary ZIP + a Markdown README, not a single JSON
        artifact, so we override with the shared path-traversal guard plus a
        readability check of ``release.zip``.
        """
        self._validate_output_paths(project, result)
        zip_path = project.stage_dir(self.id) / self.ZIP_REL
        if not zipfile.is_zipfile(zip_path):
            raise StageOutputInvalid(
                f"stage {self.id!r} output {self.ZIP_REL!r} is not a valid ZIP archive"
            )
