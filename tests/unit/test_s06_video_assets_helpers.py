"""Unit tests for `VideoAssetsStage` pure helpers (Slice 13).

These exercise the stage's mode-resolution, cost-gate, input-guard, and
output-validation branches WITHOUT any external client (no ffmpeg, no Gemini,
no Veo) — they construct the stage directly and call its methods against a
template-seeded `make_project` fixture.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from shipcast.cost import IMAGEN_IMAGE_USD, VEO_FAST_CLIP_USD
from shipcast.errors import StageInputMissing, StageOutputInvalid
from shipcast.manifest import StageStatus
from shipcast.project import Project
from shipcast.stage import StageResult
from shipcast.stages.s06_video_assets import VideoAssetsStage


def _write_input(project: Project, *, video_mode: str) -> None:
    project.input_path.write_text(
        yaml.safe_dump({"video_mode": video_mode}), encoding="utf-8"
    )


def test_resolve_mode_standard_default(make_project: Callable[..., Project]) -> None:
    project = make_project()
    stage = VideoAssetsStage()
    # No input.yaml on disk → standard.
    assert stage._resolve_mode(project) == "standard"


def test_resolve_mode_premium_from_input(make_project: Callable[..., Project]) -> None:
    project = make_project()
    _write_input(project, video_mode="premium")
    assert VideoAssetsStage()._resolve_mode(project) == "premium"


def test_no_veo_forces_standard(make_project: Callable[..., Project]) -> None:
    project = make_project()
    _write_input(project, video_mode="premium")
    assert VideoAssetsStage(no_veo=True)._resolve_mode(project) == "standard"


def test_next_call_cost_premium_is_veo(make_project: Callable[..., Project]) -> None:
    project = make_project()
    _write_input(project, video_mode="premium")
    assert VideoAssetsStage().next_call_cost_usd(project) == VEO_FAST_CLIP_USD


def test_next_call_cost_standard_is_imagen(make_project: Callable[..., Project]) -> None:
    project = make_project()
    _write_input(project, video_mode="standard")
    assert VideoAssetsStage().next_call_cost_usd(project) == IMAGEN_IMAGE_USD


def test_next_call_cost_no_veo_premium_is_imagen(
    make_project: Callable[..., Project],
) -> None:
    project = make_project()
    _write_input(project, video_mode="premium")
    assert VideoAssetsStage(no_veo=True).next_call_cost_usd(project) == IMAGEN_IMAGE_USD


def test_load_storyboard_missing_raises(make_project: Callable[..., Project]) -> None:
    project = make_project()
    with pytest.raises(StageInputMissing):
        VideoAssetsStage()._load_storyboard(project)


def test_validate_outputs_bad_json_raises(
    make_project: Callable[..., Project],
) -> None:
    project = make_project()
    stage = VideoAssetsStage()
    va_dir = project.stage_dir(stage.id)
    va_dir.mkdir(parents=True, exist_ok=True)
    (va_dir / "clips.json").write_text("{not json", encoding="utf-8")
    result = StageResult(
        status=StageStatus.DONE, outputs=(Path(stage.id) / "clips.json",)
    )
    with pytest.raises(StageOutputInvalid):
        stage.validate_outputs(project, result)


def test_validate_outputs_schema_violation_raises(
    make_project: Callable[..., Project],
) -> None:
    project = make_project()
    stage = VideoAssetsStage()
    va_dir = project.stage_dir(stage.id)
    va_dir.mkdir(parents=True, exist_ok=True)
    # `clips` empty → VideoBeats validator rejects it.
    bad: dict[str, Any] = {"mode": "standard", "clips": []}
    (va_dir / "clips.json").write_text(json.dumps(bad), encoding="utf-8")
    result = StageResult(
        status=StageStatus.DONE, outputs=(Path(stage.id) / "clips.json",)
    )
    with pytest.raises(StageOutputInvalid):
        stage.validate_outputs(project, result)
