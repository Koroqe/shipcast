"""Direct-unit tests for `BrandStage` internals (Slice 10).

Exercises the stage's branch logic without the CLI dispatcher: the hint path,
the no-hint/no-url error, the operator-supplied-style-sheet skip, the SVG-logo
placeholder fallback, validate_outputs failure, and the brand_slug reader. These
complement the CLI-driven integration tests in
`tests/integration/test_s03_brand.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from shipcast.brand.extractor import transparent_1x1_png
from shipcast.errors import StageOutputInvalid
from shipcast.schemas import BrandProposal
from shipcast.stage import StageResult, StageStatus
from shipcast.stages.s03_brand import BrandStage

REAL_PNG = transparent_1x1_png()


def _seed_pack(
    tmp_path: Path,
    *,
    logo: str = "svg",
    palette_hint: dict[str, str] | None = None,
    style_sheet: bool = False,
) -> None:
    # The Project.root used by _make_project is tmp_path/"projects"; the brand
    # pack lives under <root>/_brand/<slug>/.
    root = tmp_path / "projects" / "_brand" / "test-brand"
    (root / "fonts").mkdir(parents=True, exist_ok=True)
    (root / "voice.md").write_text("caption_mode: chip\n", encoding="utf-8")
    (root / "fonts" / "Inter.ttf").write_bytes(b"TTF")
    if logo == "svg":
        (root / "logo.svg").write_text("<svg/>", encoding="utf-8")
    elif logo == "png":
        (root / "logo.png").write_bytes(REAL_PNG)
    if palette_hint is not None:
        (root / "palette.hint.json").write_text(json.dumps(palette_hint), encoding="utf-8")
    if style_sheet:
        (root / "style_sheet.png").write_bytes(REAL_PNG)


def _make_project(
    tmp_path: Path, *, live_url: str | None, repo_path: Path | None = None
) -> Any:
    """Build a minimal Project-like object the stage can read.

    Only the attributes BrandStage touches are provided: root, path,
    input_path, stage_dir, artifact_path. Tests that call ``run()`` must
    monkeypatch ``schemas.ALLOWED_REPO_ROOT`` and pass a ``repo_path`` under it
    (with a CHANGELOG.md) so the full InputYaml validation in ``run`` passes.
    """
    from dataclasses import dataclass, field

    projects_root = tmp_path / "projects"
    slug = "p"
    project_path = projects_root / slug
    (project_path / "03_brand").mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {
        "repo_path": str(repo_path) if repo_path is not None else "/x",
        "entry_heading": "h",
        "brand_slug": "test-brand",
        "video_mode": "standard",
    }
    if live_url is not None:
        data["live_url"] = live_url
    import yaml

    (project_path / "input.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    @dataclass
    class _Settings:
        gemini_image_model: str = "gemini-3-pro-image-preview"

    @dataclass
    class _P:
        root: Path
        path: Path
        input_path: Path
        settings: _Settings = field(default_factory=_Settings)

        def stage_dir(self, stage_id: str) -> Path:
            return self.path / stage_id

        def artifact_path(self, stage_id: str, name: str) -> Path:
            return self.path / stage_id / name

    return _P(root=projects_root, path=project_path, input_path=project_path / "input.yaml")


def _clients(*, gemini: MagicMock | None = None, playwright: MagicMock | None = None) -> Any:
    class _B:
        def __init__(self) -> None:
            self.gemini = gemini or MagicMock()
            self.playwright = playwright or MagicMock()

    return _B()


def _stage(clients: Any) -> BrandStage:
    return BrandStage(clients_factory=lambda project: clients)


def _valid_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a repo with a CHANGELOG under a monkeypatched allowed root."""
    import shipcast.schemas as schemas

    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", allowed)
    repo = allowed / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    return repo


# --------------------------------------------------------------------------- #
# hint path (no live_url) — svg logo → placeholder + style sheet from Gemini
# --------------------------------------------------------------------------- #


def test_hint_path_svg_logo_writes_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hint = {"primary": "#FF0000", "accent": "#00FF00", "neutral": "#0000FF"}
    _seed_pack(tmp_path, logo="svg", palette_hint=hint)
    repo = _valid_repo(tmp_path, monkeypatch)
    project = _make_project(tmp_path, live_url=None, repo_path=repo)
    gemini = MagicMock()
    gemini.generate_image.return_value = REAL_PNG
    playwright = MagicMock()
    playwright.extract_css_palette.side_effect = AssertionError("must not call")
    result = _stage(_clients(gemini=gemini, playwright=playwright)).run(project)

    assert result.status == StageStatus.DONE
    proposal = BrandProposal.model_validate_json(
        (project.path / "03_brand" / "proposal.json").read_text()
    )
    assert proposal.palette == ["#FF0000", "#00FF00", "#0000FF"]
    # SVG logo on the hint/no-url path → transparent placeholder, not detected.
    assert proposal.logo_detected is False
    playwright.extract_css_palette.assert_not_called()
    gemini.generate_image.assert_called_once()


def test_hint_path_png_logo_copied_and_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hint = {"primary": "#111111", "accent": "#222222", "neutral": "#333333"}
    _seed_pack(tmp_path, logo="png", palette_hint=hint, style_sheet=True)
    repo = _valid_repo(tmp_path, monkeypatch)
    project = _make_project(tmp_path, live_url=None, repo_path=repo)
    gemini = MagicMock()
    gemini.generate_image.side_effect = AssertionError("style sheet supplied")
    result = _stage(_clients(gemini=gemini)).run(project)

    proposal = BrandProposal.model_validate_json(
        (project.path / "03_brand" / "proposal.json").read_text()
    )
    assert proposal.logo_detected is True
    # operator-supplied style_sheet.png → no Gemini call, cost 0.
    gemini.generate_image.assert_not_called()
    assert result.metrics["cost_usd"] == 0.0
    assert result.metrics["palette_from_hint"] is True


# --------------------------------------------------------------------------- #
# no hint + no live_url → ValueError (cannot extract palette)
# --------------------------------------------------------------------------- #


def test_no_hint_no_live_url_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pack(tmp_path, logo="svg")  # no palette hint
    repo = _valid_repo(tmp_path, monkeypatch)
    project = _make_project(tmp_path, live_url=None, repo_path=repo)
    with pytest.raises(ValueError, match=r"palette\.hint\.json|live_url"):
        _stage(_clients()).run(project)


# --------------------------------------------------------------------------- #
# style-sheet prompt with an empty palette (defensive branch)
# --------------------------------------------------------------------------- #


def test_style_sheet_prompt_empty_palette() -> None:
    prompt = BrandStage._style_sheet_prompt([])
    assert "balanced brand palette" in prompt
    prompt2 = BrandStage._style_sheet_prompt(["#abcdef"])
    assert "#abcdef" in prompt2


# --------------------------------------------------------------------------- #
# brand_slug reader — missing field
# --------------------------------------------------------------------------- #


def test_read_brand_slug_missing_raises(tmp_path: Path) -> None:
    project = _make_project(tmp_path, live_url=None)
    import yaml

    project.input_path.write_text(
        yaml.safe_dump({"repo_path": "/x", "entry_heading": "h", "video_mode": "standard"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="brand_slug"):
        BrandStage()._read_brand_slug(project)


def test_read_raw_input_missing_file_raises(tmp_path: Path) -> None:
    from shipcast.errors import StageInputMissing

    project = _make_project(tmp_path, live_url=None)
    project.input_path.unlink()
    with pytest.raises(StageInputMissing):
        BrandStage()._read_raw_input(project)


def test_read_raw_input_non_mapping_raises(tmp_path: Path) -> None:
    project = _make_project(tmp_path, live_url=None)
    project.input_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML mapping"):
        BrandStage()._read_raw_input(project)


# --------------------------------------------------------------------------- #
# additional_input_paths — happy + swallow-on-error
# --------------------------------------------------------------------------- #


def test_additional_input_paths_lists_pack_files(tmp_path: Path) -> None:
    _seed_pack(tmp_path, logo="svg")
    project = _make_project(tmp_path, live_url=None)
    paths = list(BrandStage().additional_input_paths(project))
    names = {p.name for p in paths}
    assert {"voice.md", "logo.svg", "Inter.ttf"} <= names


def test_additional_input_paths_swallows_incomplete_pack(tmp_path: Path) -> None:
    # No brand pack at all → returns () rather than raising (audit-only hashing).
    project = _make_project(tmp_path, live_url=None)
    assert list(BrandStage().additional_input_paths(project)) == []


# --------------------------------------------------------------------------- #
# validate_outputs — schema failure on a corrupt proposal.json
# --------------------------------------------------------------------------- #


def test_validate_outputs_rejects_bad_proposal(tmp_path: Path) -> None:
    project = _make_project(tmp_path, live_url=None)
    bdir = project.path / "03_brand"
    (bdir / "proposal.json").write_text(
        '{"palette": ["not-a-hex"], "font_family": "X", "logo_detected": true}',
        encoding="utf-8",
    )
    (bdir / "logo.png").write_bytes(REAL_PNG)
    (bdir / "style_sheet.png").write_bytes(REAL_PNG)
    (bdir / "voice.md").write_text("x", encoding="utf-8")
    result = StageResult(
        status=StageStatus.DONE,
        outputs=(
            Path("03_brand") / "proposal.json",
            Path("03_brand") / "logo.png",
            Path("03_brand") / "style_sheet.png",
            Path("03_brand") / "voice.md",
        ),
    )
    with pytest.raises(StageOutputInvalid, match="schema validation"):
        BrandStage().validate_outputs(project, result)


def test_validate_outputs_rejects_non_json(tmp_path: Path) -> None:
    project = _make_project(tmp_path, live_url=None)
    bdir = project.path / "03_brand"
    (bdir / "proposal.json").write_text("not json", encoding="utf-8")
    for name in ("logo.png", "style_sheet.png"):
        (bdir / name).write_bytes(REAL_PNG)
    (bdir / "voice.md").write_text("x", encoding="utf-8")
    result = StageResult(
        status=StageStatus.DONE,
        outputs=(
            Path("03_brand") / "proposal.json",
            Path("03_brand") / "logo.png",
            Path("03_brand") / "style_sheet.png",
            Path("03_brand") / "voice.md",
        ),
    )
    with pytest.raises(StageOutputInvalid, match="not valid JSON"):
        BrandStage().validate_outputs(project, result)
