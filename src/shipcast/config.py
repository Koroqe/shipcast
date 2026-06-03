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
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    voice_id: str = "UGTtbzgh3HObxRjWaSpr"
    anthropic_model: str = "claude-opus-4-7"
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
    #: ``anthropic_model`` / ``elevenlabs_model`` / ``whisperx_model``).
    gemini_image_model: str = "gemini-3-pro-image-preview"
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
    # Note: anthropic_api_key was removed when stage 02 switched from the
    # Anthropic SDK to the `claude` CLI subprocess (uses the operator's
    # subscription). The remaining keys land when their stages are implemented.
    elevenlabs_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")

    @classmethod
    def from_files(
        cls,
        config_path: Path | None = None,
        env_path: Path | None = None,
    ) -> Settings:
        """Construct Settings, loading public defaults from `config_path` if it exists.

        Secrets come from process environment (or `env_path` if supplied).
        Nested TOML sections (e.g. a future `[images]` block) are SKIPPED
        — only top-level scalar entries become Settings kwargs.
        """
        kwargs: dict[str, Any] = {}
        if config_path is not None and config_path.is_file():
            with config_path.open("rb") as f:
                toml_data = tomllib.load(f)
            for key, value in toml_data.items():
                if isinstance(value, dict):
                    continue  # nested sections are forward-compatible seams
                kwargs[key] = value
        if env_path is not None:
            return cls(_env_file=str(env_path), **kwargs)  # type: ignore[call-arg]
        return cls(**kwargs)

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
