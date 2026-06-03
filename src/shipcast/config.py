"""Centralized configuration via pydantic-settings.

`Settings` loads public defaults from `config.toml` (passed explicitly to
`Settings.from_files`) and secrets from environment variables (or a `.env`
file). The split is intentional: secrets live in `.env`, which is gitignored;
public defaults live in `config.toml`, which is committed.

CRITICAL: `Settings.public_dict()` is the ONLY shape that may be persisted
into `manifest.json` (via `config_snapshot`). It programmatically excludes
every field annotated as `SecretStr`, so a future-added secret can never
silently leak — adding the field is sufficient, no allowlist edit required.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Mode → project cost cap (USD). Standard launches are capped at $3; premium
#: marquee launches (one Veo 3 Fast clip) at $8. `max_cost_usd_per_project`
#: derives from `video_mode` via this table.
_MODE_CAP_USD: dict[str, float] = {"standard": 3.0, "premium": 8.0}

#: How shipcast's NESTED `config.toml` sections map onto flat Settings field
#: names. `Settings.from_files` walks each `[section].key` and, when the pair
#: is listed here, assigns the value to the mapped Settings field. Keys NOT in
#: this map are ignored (forward-compatible seam) so adding a new TOML key is
#: non-breaking. `None` means "the field name equals the TOML key".
_TOML_SECTION_FIELD_MAP: dict[str, dict[str, str | None]] = {
    "models": {
        "gemini_image_model": None,
        "gemini_multimodal_model": None,
        "gemini_veo_model": None,
    },
    "voice": {
        "elevenlabs_voice_id": "voice_id",
        "elevenlabs_model": None,
    },
    "video": {
        "default_mode": "video_mode",
        "showcase_min_sec": None,
        "showcase_max_sec": None,
        "loop_sec": None,
        "beats_per_video": None,
    },
    "cost": {
        "max_usd_standard": None,
        "max_usd_premium": None,
        "veo_fast_clip_usd": None,
        "imagen_image_usd": None,
        "gemini_multimodal_call_usd": None,
        "elevenlabs_per_minute_usd": None,
    },
}


class Settings(BaseSettings):
    """Shipcast configuration. Loaded from `config.toml` + env (with optional `.env`)."""

    model_config = SettingsConfigDict(
        env_file=None,  # `from_files` opts in explicitly
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # ── Public fields (from config.toml or constructor args) ──────────────
    target_duration_sec: int = 300
    duration_tolerance_sec: int = 5
    image_cut_min_sec: float = 1.0
    image_cut_max_sec: float = 5.0
    #: ElevenLabs voice id. Sourced from `[voice].elevenlabs_voice_id` in
    #: `config.toml`; the operator typically sets it per brand. When the TOML
    #: value is empty the field default below applies.
    voice_id: str = "UGTtbzgh3HObxRjWaSpr"
    elevenlabs_model: str = "eleven_v3"
    #: ElevenLabs voice_settings — tuned for an articulate, dynamically
    #: intonated narrator on the chosen voice. The Stage 03 client forwards
    #: these verbatim into the ElevenLabs `voice_settings` payload. They are
    #: flat top-level keys because `Settings.from_files` skips nested TOML
    #: sections (mirrors the precedent set by `voice_id` / `elevenlabs_model`).
    voice_stability: float = 0.20
    voice_similarity_boost: float = 0.85
    voice_style: float = 0.80
    voice_use_speaker_boost: bool = True
    #: ElevenLabs pace knob (eleven_v3 + multilingual_v2 + turbo_v2_5 family).
    #: API range is roughly 0.7..1.2 where 1.0 = neutral pace. Defaults to 1.20
    #: (API max) which pairs with the moderate-expressive preset above to
    #: produce energetic delivery that lands near the 5-minute target on a
    #: 625-word script. Threaded into voice_settings alongside
    #: stability/style/similarity/use_speaker_boost.
    voice_speed: float = 1.20
    whisperx_model: str = "base.en"
    #: Stage 09 image-generation model — Google Gemini "Nano Banana 2"
    #: family via the AI Studio REST surface. Read LIVE from
    #: ``project.settings`` (tuning knob, matches the precedent set by
    #: ``elevenlabs_model`` / ``whisperx_model``).
    gemini_image_model: str = "gemini-3-pro-image-preview"
    #: Stage 02 multimodal narrative model (Gemini). Sourced from
    #: `[models].gemini_multimodal_model`.
    gemini_multimodal_model: str = "gemini-2.5-pro"
    #: Stage 06 premium-mode video model (Veo 3 Fast). Sourced from
    #: `[models].gemini_veo_model`.
    gemini_veo_model: str = "veo-3-fast"

    #: Video render mode for the project. Sourced from `[video].default_mode`;
    #: the per-project `input.yaml` (Slice 3) ultimately overrides this. Drives
    #: `max_cost_usd_per_project`.
    video_mode: Literal["standard", "premium"] = "standard"
    showcase_min_sec: int = 15
    showcase_max_sec: int = 25
    loop_sec: int = 6
    beats_per_video: int = 4

    # ── Cost caps + per-tool unit costs (from config.toml `[cost]`) ────────
    #: Mode caps. `max_cost_usd_per_project` selects between these by mode.
    max_usd_standard: float = 3.0
    max_usd_premium: float = 8.0
    #: Per-tool unit costs mirror the constants in `cost.py` (single source of
    #: truth for the dispatcher's pre-call cap gate is `cost.py`; these exist so
    #: operators can audit the prices alongside the caps in one file).
    veo_fast_clip_usd: float = 3.20
    imagen_image_usd: float = 0.04
    gemini_multimodal_call_usd: float = 0.01
    elevenlabs_per_minute_usd: float = 0.30

    #: Stage 08's image-consistency strategy. v1 only supports
    #: ``"prompt_seed"`` (same style tag suffix + per-scene deterministic
    #: seed). Future strategies (``"flux_redux"``, ``"lora"``) deferred.
    #: Stored as a FLAT top-level key in ``config.toml`` because
    #: ``Settings.from_files`` skips nested TOML sections.
    consistency_strategy: str = "prompt_seed"
    #: Stage 09's image-anchor strategy. v1 only supports ``"scene_0"``
    #: (generate scene 0 first; pass its bytes as the inline_data
    #: reference image for scenes 1..N). Future strategies
    #: (``"operator_reference"``, ``"no_anchor"``) deferred. Stored as a
    #: FLAT top-level key (same reason as ``consistency_strategy``) and
    #: read FROZEN from ``config_snapshot`` (project-birth contract).
    image_anchor_strategy: str = "scene_0"

    # ── Secrets (from env / .env / constructor — NEVER from config.toml) ──
    # All three are `SecretStr`, populated from the env vars ANTHROPIC_API_KEY /
    # ELEVENLABS_API_KEY / GEMINI_API_KEY (pydantic-settings uppercases the
    # field name). `public_dict()` excludes every SecretStr by TYPE, so none of
    # these ever reach `config_snapshot`. ANTHROPIC_API_KEY is retained here for
    # the SSRF/secret-exclusion contract even though the `claude -p` subprocess
    # path authenticates via the operator's CLI subscription, not this key.
    anthropic_api_key: SecretStr = SecretStr("")
    elevenlabs_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")

    # ── Derived ───────────────────────────────────────────────────────────
    @property
    def max_cost_usd_per_project(self) -> float:
        """The project cost cap, derived from `video_mode`.

        standard → `max_usd_standard` ($3 default); premium → `max_usd_premium`
        ($8 default). The dispatcher charges paid calls against this cap.
        """
        if self.video_mode == "premium":
            return self.max_usd_premium
        return self.max_usd_standard

    @classmethod
    def from_files(
        cls,
        config_path: Path | None = None,
        env_path: Path | None = None,
    ) -> Settings:
        """Construct Settings, loading public defaults from `config_path` if it exists.

        Secrets come from process environment (or `env_path` if supplied).

        shipcast's `config.toml` is NESTED (`[cost]`, `[voice]`, `[models]`,
        `[video]`). `from_files` flattens the sections enumerated in
        `_TOML_SECTION_FIELD_MAP` onto the matching Settings fields. Top-level
        scalar entries (if any) are also accepted verbatim. Unknown nested
        sections/keys are ignored (forward-compatible seam). An empty-string
        TOML value is skipped so it never overrides a non-empty field default
        (e.g. `[voice].elevenlabs_voice_id = ""` keeps the default voice).
        """
        kwargs: dict[str, Any] = {}
        if config_path is not None and config_path.is_file():
            with config_path.open("rb") as f:
                toml_data = tomllib.load(f)
            for key, value in toml_data.items():
                if isinstance(value, dict):
                    cls._collect_section(key, value, kwargs)
                    continue
                kwargs[key] = value
        if env_path is not None:
            return cls(_env_file=str(env_path), **kwargs)  # type: ignore[call-arg]
        return cls(**kwargs)

    @staticmethod
    def _collect_section(
        section: str, values: dict[str, Any], kwargs: dict[str, Any]
    ) -> None:
        """Flatten one mapped `[section]` of `config.toml` into `kwargs`."""
        field_map = _TOML_SECTION_FIELD_MAP.get(section)
        if field_map is None:
            return  # unmapped section: forward-compatible seam
        for toml_key, value in values.items():
            if toml_key not in field_map:
                continue  # unmapped key within a known section
            if isinstance(value, str) and value == "":
                continue  # empty string never overrides a field default
            field_name = field_map[toml_key] or toml_key
            kwargs[field_name] = value

    def public_dict(self) -> dict[str, Any]:
        """Return a serializable dict excluding every `SecretStr` field.

        This is the ONLY shape that may be passed to `Project.create`'s
        `config_snapshot` argument. Programmatic exclusion: walks
        `type(self).model_fields` and drops any field whose annotation is
        `SecretStr` (or a subclass). Adding a new secret field requires no
        allowlist edit — the exclusion happens by type, not by name.
        """
        secret_fields = {
            name
            for name, field in type(self).model_fields.items()
            if _is_secret_annotation(field.annotation)
        }
        return self.model_dump(mode="json", exclude=secret_fields)


def _is_secret_annotation(annotation: Any) -> bool:
    """Return True iff `annotation` is `SecretStr` or a subclass."""
    return isinstance(annotation, type) and issubclass(annotation, SecretStr)
