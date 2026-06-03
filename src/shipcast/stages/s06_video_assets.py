"""Stage 06 — video assets (both modes).

Turns the approved showcase ``Storyboard`` (``05_script/storyboard.json``) into
four 1080x1920 h264 MP4 clips under ``06_video_assets/``:

* **standard** (default): every beat → Gemini Imagen still (9:16) + ffmpeg
  Ken-Burns pan/zoom. 4 clips, each 3-5 s. Veo is never touched.
* **premium**: beat[0] → Veo 3 Fast 8 s hero clip (conditioned on
  ``03_brand/style_sheet.png`` when present); beats[1..3] → Imagen + Ken-Burns.

The resolved render mode comes from ``input.yaml.video_mode`` (the authoritative
per-project knob the operator sets via ``shipcast pick --video-mode``). The
``--no-veo`` flag forces the standard (all Ken-Burns) path even for a premium
project — wired via the stage's ``no_veo`` constructor argument, NOT by importing
``cli`` (the stage stays pure).

Error handling
--------------
* Imagen transient errors (429/5xx) are retried up to ``_IMAGE_RETRIES`` times;
  exhaustion → :class:`GeminiImageGenFailed`. A :class:`GeminiSafetyBlocked`
  (HTTP-200 content-policy block) is re-raised UNWRAPPED so the dispatcher
  records ``error.type == "GeminiSafetyBlocked"`` (UC-7-E1 GAP closure).
* ``VeoSafetyBlocked`` on beat[0] → SILENT per-beat fallback to Imagen +
  Ken-Burns (the run still succeeds). SECURITY: the blocked hero prompt is
  NEVER logged.
* ``VeoQuotaExceeded`` / ``VeoTimeout`` → HARD failure; no further beats are
  rendered.
* Each written clip is validated with ``ffprobe`` (h264 + 1080x1920);
  a mismatch → :class:`ClipValidationFailed`.

Cost (MINOR-2 invariant)
------------------------
This is the first PAID stage. Cost is recorded ONLY in the returned
``StageResult.metrics["cost_usd"]`` and is written into the manifest by the
dispatcher on the DONE transition. The stage NEVER writes cost into the manifest
mid-run, so a FAILED paid run carries NO ``cost_usd`` (the dispatcher's
``_record_failure`` omits metrics). ``next_call_cost_usd`` returns the cost of
the single MOST-EXPENSIVE call the run will make (premium → one Veo clip;
standard → one Imagen still) for the dispatcher's pre-call cost-cap gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from shipcast.clients import ffmpeg_client as _ffmpeg
from shipcast.cost import IMAGEN_IMAGE_USD, VEO_FAST_CLIP_USD
from shipcast.errors import (
    ClipValidationFailed,
    GeminiImageGenFailed,
    GeminiSafetyBlocked,
    GeminiTransientError,
    StageInputMissing,
    StageOutputInvalid,
    VeoSafetyBlocked,
)
from shipcast.manifest import StageStatus, dump_json_canonical
from shipcast.schemas import Storyboard, StoryboardBeat, VideoBeats
from shipcast.stage import StageResult
from shipcast.stages._base import BaseStage

if TYPE_CHECKING:
    from collections.abc import Callable

    from shipcast.clients.gemini_client import AspectRatio
    from shipcast.project import Project

#: Imagen still retry budget (transient 429/5xx only).
_IMAGE_RETRIES: int = 3

#: Veo conditioning image (optional). Present iff s03_brand emitted one.
_STYLE_SHEET_REL: str = "03_brand/style_sheet.png"

#: Expected clip geometry/codec (every clip, both modes).
_EXPECTED_CODEC: str = "h264"
_EXPECTED_WIDTH: int = 1080
_EXPECTED_HEIGHT: int = 1920


# --------------------------------------------------------------------------- #
# Clients bundle Protocol (structural; mocked in tests)
# --------------------------------------------------------------------------- #


@runtime_checkable
class _GeminiLike(Protocol):
    def generate_image(
        self,
        prompt: str,
        *,
        model: str,
        seed: int,
        reference_image_bytes: bytes | None = ...,
        aspect_ratio: AspectRatio = ...,
    ) -> bytes: ...


@runtime_checkable
class _VeoLike(Protocol):
    def generate_clip(
        self,
        prompt: str,
        *,
        model: str,
        output_path: Path,
        conditioning_image: Path | None = ...,
    ) -> Path: ...


class _ClientsBundle(Protocol):
    @property
    def gemini(self) -> _GeminiLike: ...

    @property
    def veo(self) -> _VeoLike: ...


def _default_clients_factory(project: Project) -> _ClientsBundle:
    """Construct the real Gemini + Veo clients lazily inside ``run()``.

    Both heavy clients import ``requests`` only inside their methods, preserving
    import-purity. The Veo client is only invoked in premium mode, but it is
    constructed here regardless — its ``__init__`` merely validates the key.
    """
    from shipcast.clients.gemini_client import GeminiClient
    from shipcast.clients.veo_client import VeoClient

    gemini = GeminiClient(api_key=project.settings.gemini_api_key)
    veo = VeoClient(api_key=project.settings.gemini_api_key)

    class _Bundle:
        def __init__(self) -> None:
            self.gemini: _GeminiLike = gemini
            self.veo: _VeoLike = veo

    return _Bundle()


class VideoAssetsStage(BaseStage):
    """Produce four ``06_video_assets/beat_NN.mp4`` clips + ``clips.json``."""

    id: ClassVar[str] = "06_video_assets"
    requires: ClassVar[tuple[str, ...]] = ("05_script",)
    output_schema: ClassVar[type[VideoBeats]] = VideoBeats
    requires_ffmpeg: ClassVar[bool] = True
    review_checklist_items: ClassVar[tuple[str, ...]] = (
        "Watch each clip — confirm it matches its beat's image_prompt and reads "
        "on-brand.",
        "Confirm the hero clip lands the hook (premium: the Veo motion clip; "
        "standard: the first Ken-Burns clip).",
        "Verify every clip is vertical 1080x1920 and the motion is smooth, not "
        "jittery.",
    )

    CLIPS_FILENAME: ClassVar[str] = "clips.json"
    STORYBOARD_REL: ClassVar[str] = "05_script/storyboard.json"

    def __init__(
        self,
        *,
        clients_factory: Callable[[Project], _ClientsBundle] | None = None,
        no_veo: bool = False,
    ) -> None:
        self._clients_factory: Callable[[Project], _ClientsBundle] = (
            clients_factory or _default_clients_factory
        )
        #: When True, force the standard (all Ken-Burns) path even in premium
        #: mode (``--no-veo``). Veo is never constructed-into-use nor called.
        self._no_veo = no_veo

    # ------------------------------------------------------------- cost gate
    def next_call_cost_usd(self, project: Project) -> float:
        """Cost of the single MOST-EXPENSIVE call this run will make.

        Premium (and not ``--no-veo``) → one Veo clip ($3.20). Otherwise → one
        Imagen still ($0.04). The dispatcher gates ``accumulated + this > cap``
        BEFORE constructing any paid client.
        """
        if self._resolve_mode(project) == "premium":
            return VEO_FAST_CLIP_USD
        return IMAGEN_IMAGE_USD

    # ------------------------------------------------------------- mode
    def _resolve_mode(self, project: Project) -> str:
        """The effective render mode: ``premium`` only if input.yaml says so AND
        ``--no-veo`` was not passed; otherwise ``standard``.
        """
        if self._no_veo:
            return "standard"
        return self._read_input_mode(project)

    @staticmethod
    def _read_input_mode(project: Project) -> str:
        """Read ``video_mode`` from ``input.yaml`` (default ``standard``)."""
        import yaml

        path = project.input_path
        if not path.is_file():
            return "standard"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("video_mode") == "premium":
            return "premium"
        return "standard"

    # ------------------------------------------------------------- inputs
    def _load_storyboard(self, project: Project) -> Storyboard:
        path = project.path / self.STORYBOARD_REL
        if not path.is_file():
            raise StageInputMissing(
                f"stage {self.id!r} requires {self.STORYBOARD_REL} to exist"
            )
        return Storyboard.model_validate_json(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------- run
    def run(self, project: Project) -> StageResult:
        clients = self._clients_factory(project)
        storyboard = self._load_storyboard(project)
        mode = self._resolve_mode(project)
        stage_dir = project.stage_dir(self.id)
        stage_dir.mkdir(parents=True, exist_ok=True)

        cost = 0.0
        records: list[dict[str, Any]] = []
        outputs: list[Path] = []

        image_model = project.settings.gemini_image_model
        for index, beat in enumerate(storyboard.beats):
            clip_path = stage_dir / f"beat_{index:02d}.mp4"
            source, beat_cost = self._dispatch_beat(
                project, clients, beat, index, mode, clip_path, image_model
            )
            cost += beat_cost
            self._validate_clip(clip_path)
            records.append(
                {
                    "index": index,
                    "filename": clip_path.name,
                    "source": source,
                    "duration_sec": (
                        8.0 if source == "veo" else float(beat.duration_sec)
                    ),
                }
            )
            outputs.append(Path(self.id) / clip_path.name)

        clips = VideoBeats.model_validate({"mode": mode, "clips": records})
        clips_path = project.artifact_path(self.id, self.CLIPS_FILENAME)
        clips_path.write_text(
            dump_json_canonical(clips.model_dump(mode="json")), encoding="utf-8"
        )
        outputs.append(Path(self.id) / self.CLIPS_FILENAME)

        return StageResult(
            status=StageStatus.DONE,
            outputs=tuple(outputs),
            metrics={"cost_usd": round(cost, 4), "mode": mode, "clips": len(records)},
        )

    # ------------------------------------------------------------- dispatch
    def _dispatch_beat(
        self,
        project: Project,
        clients: _ClientsBundle,
        beat: StoryboardBeat,
        index: int,
        mode: str,
        clip_path: Path,
        image_model: str,
    ) -> tuple[str, float]:
        """Render one beat; return ``(source, cost_usd)``.

        Premium beat[0] tries Veo first and silently falls back to Ken-Burns on a
        :class:`VeoSafetyBlocked`. ``VeoQuotaExceeded`` / ``VeoTimeout`` propagate
        (HARD failure handled by the dispatcher).
        """
        if mode == "premium" and index == 0:
            try:
                return self._render_veo_clip(project, clients, beat, clip_path)
            except VeoSafetyBlocked:
                # SECURITY: do NOT log the blocked prompt. Fall back silently.
                return self._render_kenburns_clip(clients, beat, clip_path, image_model)
        return self._render_kenburns_clip(clients, beat, clip_path, image_model)

    def _render_veo_clip(
        self,
        project: Project,
        clients: _ClientsBundle,
        beat: StoryboardBeat,
        clip_path: Path,
    ) -> tuple[str, float]:
        """Generate the premium hero clip via Veo 3 Fast → ``(source, cost)``."""
        conditioning = project.path / _STYLE_SHEET_REL
        clients.veo.generate_clip(
            beat.image_prompt,
            model=project.settings.gemini_veo_model,
            output_path=clip_path,
            conditioning_image=conditioning if conditioning.is_file() else None,
        )
        return "veo", VEO_FAST_CLIP_USD

    def _render_kenburns_clip(
        self,
        clients: _ClientsBundle,
        beat: StoryboardBeat,
        clip_path: Path,
        image_model: str,
    ) -> tuple[str, float]:
        """Imagen still (9:16) + ffmpeg Ken-Burns → ``(source, cost)``."""
        still_bytes = self._generate_still_with_retry(clients, beat, image_model)
        still_path = clip_path.with_suffix(".png")
        still_path.write_bytes(still_bytes)
        try:
            _ffmpeg.ken_burns_clip(
                still_path=still_path,
                duration_sec=float(beat.duration_sec),
                output_path=clip_path,
            )
        finally:
            still_path.unlink(missing_ok=True)
        return "ken_burns", IMAGEN_IMAGE_USD

    def _generate_still_with_retry(
        self, clients: _ClientsBundle, beat: StoryboardBeat, image_model: str
    ) -> bytes:
        """Call Imagen with a bounded retry on transient errors.

        A :class:`GeminiSafetyBlocked` is re-raised UNWRAPPED (GAP closure); any
        other non-transient error and transient-retry exhaustion are wrapped in
        :class:`GeminiImageGenFailed`.
        """
        last_exc: BaseException | None = None
        for _attempt in range(_IMAGE_RETRIES):
            try:
                return clients.gemini.generate_image(
                    beat.image_prompt,
                    model=image_model,
                    seed=0,
                    aspect_ratio="9:16",
                )
            except GeminiSafetyBlocked:
                # Surface the safety-block subtype directly so error.type is
                # "GeminiSafetyBlocked", not the GeminiImageGenFailed wrapper.
                raise
            except GeminiTransientError as exc:
                last_exc = exc
                continue
            except Exception as exc:  # non-transient, non-safety → wrap immediately
                raise GeminiImageGenFailed(0, exc) from exc
        assert last_exc is not None  # loop ran at least once
        raise GeminiImageGenFailed(0, last_exc) from last_exc

    # ------------------------------------------------------------- validation
    def _validate_clip(self, clip_path: Path) -> None:
        """ffprobe the written clip; raise ``ClipValidationFailed`` on mismatch."""
        probe = _ffmpeg.probe_video(clip_path)
        if (
            probe.codec_name != _EXPECTED_CODEC
            or probe.width != _EXPECTED_WIDTH
            or probe.height != _EXPECTED_HEIGHT
        ):
            raise ClipValidationFailed(
                filename=clip_path.name,
                codec=probe.codec_name,
                width=probe.width,
                height=probe.height,
            )

    # ------------------------------------------------------------- outputs
    def validate_outputs(self, project: Project, result: StageResult) -> None:
        """Path-safety on every output + schema-check ``clips.json`` only.

        Outputs are 4 MP4s + one JSON, so the default single-output schema check
        does not apply; we run the shared path-traversal guard over all outputs
        and validate ``clips.json`` against :class:`VideoBeats` explicitly.
        """
        import json

        from pydantic import ValidationError

        self._validate_output_paths(project, result)
        clips_rel = Path(self.id) / self.CLIPS_FILENAME
        full = (project.path / clips_rel).resolve()
        try:
            data = json.loads(full.read_text(encoding="utf-8"))
            VideoBeats.model_validate(data)
        except json.JSONDecodeError as exc:
            raise StageOutputInvalid(
                f"stage {self.id!r} output {full} is not valid JSON: {exc}"
            ) from exc
        except ValidationError as exc:
            raise StageOutputInvalid(
                f"stage {self.id!r} output {full} failed schema validation: {exc}"
            ) from exc
