"""Slice 2 — Settings secret-exclusion + nested config.toml reconciliation.

Owned TCs:
- TC-19.2: `settings.public_dict()` contains none of the 3 key NAMES or their
  values (`ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`).

Also covers the Slice-2 config reconciliation:
- `Settings.from_files` actually loads shipcast's NESTED `config.toml` values
  (`[cost]` caps, `[voice].elevenlabs_voice_id`, `[models]`).
- `max_cost_usd_per_project` is derived from `video_mode`
  ("standard" → $3, "premium" → $8).
- The 3 API keys are `SecretStr` fields read from env / .env.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from shipcast.config import Settings

# Distinctive sentinel values that must NEVER surface in public_dict output.
_ANTHROPIC = "sk-ant-SENTINEL-anthropic-0001"
_ELEVENLABS = "el-SENTINEL-elevenlabs-0002"
_GEMINI = "gm-SENTINEL-gemini-0003"

_KEY_NAMES = ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "GEMINI_API_KEY")
_SECRET_VALUES = (_ANTHROPIC, _ELEVENLABS, _GEMINI)


def _settings_with_all_keys() -> Settings:
    return Settings(
        anthropic_api_key=SecretStr(_ANTHROPIC),
        elevenlabs_api_key=SecretStr(_ELEVENLABS),
        gemini_api_key=SecretStr(_GEMINI),
    )


# --------------------------------------------------------------------------- #
# The three keys are SecretStr fields
# --------------------------------------------------------------------------- #


def test_three_api_keys_are_secretstr_fields() -> None:
    """ANTHROPIC/ELEVENLABS/GEMINI keys exist as SecretStr fields on Settings."""
    fields = Settings.model_fields
    for name in ("anthropic_api_key", "elevenlabs_api_key", "gemini_api_key"):
        assert name in fields, f"missing secret field {name!r}"
        annotation = fields[name].annotation
        assert isinstance(annotation, type) and issubclass(annotation, SecretStr)


def test_api_keys_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """SecretStr keys are populated from environment variables."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ANTHROPIC)
    monkeypatch.setenv("GEMINI_API_KEY", _GEMINI)
    s = Settings()
    assert s.anthropic_api_key.get_secret_value() == _ANTHROPIC
    assert s.gemini_api_key.get_secret_value() == _GEMINI


# --------------------------------------------------------------------------- #
# TC-19.2 — public_dict excludes every secret (by NAME and by VALUE)
# --------------------------------------------------------------------------- #


def test_tc_19_2_public_dict_excludes_secret_field_names() -> None:
    """TC-19.2: no SecretStr field NAME appears as a key in public_dict()."""
    public = _settings_with_all_keys().public_dict()
    for field_name in ("anthropic_api_key", "elevenlabs_api_key", "gemini_api_key"):
        assert field_name not in public, f"secret field {field_name!r} leaked into public_dict"


def test_tc_19_2_public_dict_excludes_secret_values() -> None:
    """TC-19.2: no secret VALUE appears anywhere in the serialized public_dict."""
    public = _settings_with_all_keys().public_dict()
    serialized = json.dumps(public)
    for value in _SECRET_VALUES:
        assert value not in serialized, f"secret value leaked into public_dict: {value!r}"


def test_tc_19_2_public_dict_excludes_uppercase_key_names() -> None:
    """TC-19.2: the uppercase env-var NAMES are also absent from the serialized form."""
    serialized = json.dumps(_settings_with_all_keys().public_dict())
    for name in _KEY_NAMES:
        assert name not in serialized


def test_public_dict_is_json_serializable_and_keeps_public_fields() -> None:
    """public_dict() round-trips through JSON and retains non-secret fields."""
    public = _settings_with_all_keys().public_dict()
    round_tripped = json.loads(json.dumps(public))
    assert round_tripped["voice_id"]  # a representative public field survives


# --------------------------------------------------------------------------- #
# Nested config.toml reconciliation
# --------------------------------------------------------------------------- #


def _write_toml(tmp_path: Path) -> Path:
    """Write a shipcast-shaped NESTED config.toml mirroring the committed one."""
    toml = tmp_path / "config.toml"
    toml.write_text(
        "\n".join(
            [
                "[models]",
                'gemini_image_model = "imagen-4.0-generate-001"',
                'gemini_multimodal_model = "gemini-2.5-pro"',
                "",
                "[voice]",
                'elevenlabs_voice_id = "VOICE-FROM-TOML"',
                'elevenlabs_model = "eleven_multilingual_v2"',
                "",
                "[video]",
                'default_mode = "premium"',
                "",
                "[cost]",
                "max_usd_standard = 3.0",
                "max_usd_premium = 8.0",
                "veo_fast_clip_usd = 3.20",
                "imagen_image_usd = 0.04",
                "gemini_multimodal_call_usd = 0.01",
                "elevenlabs_per_minute_usd = 0.30",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return toml


def test_from_files_loads_nested_voice_id(tmp_path: Path) -> None:
    """`[voice].elevenlabs_voice_id` flows into `Settings.voice_id`."""
    s = Settings.from_files(config_path=_write_toml(tmp_path))
    assert s.voice_id == "VOICE-FROM-TOML"


def test_from_files_loads_nested_models(tmp_path: Path) -> None:
    """`[models]` entries flow into the model fields."""
    s = Settings.from_files(config_path=_write_toml(tmp_path))
    assert s.gemini_image_model == "imagen-4.0-generate-001"
    assert s.gemini_multimodal_model == "gemini-2.5-pro"


def test_from_files_loads_nested_cost_caps(tmp_path: Path) -> None:
    """`[cost]` caps flow into the dedicated Settings fields."""
    s = Settings.from_files(config_path=_write_toml(tmp_path))
    assert s.max_usd_standard == 3.0
    assert s.max_usd_premium == 8.0


def test_from_files_loads_video_mode(tmp_path: Path) -> None:
    """`[video].default_mode` flows into `Settings.video_mode`."""
    s = Settings.from_files(config_path=_write_toml(tmp_path))
    assert s.video_mode == "premium"


# --------------------------------------------------------------------------- #
# max_cost_usd_per_project derives from video_mode
# --------------------------------------------------------------------------- #


def test_max_cost_standard_mode() -> None:
    """Standard mode caps the project at $3.00."""
    assert Settings(video_mode="standard").max_cost_usd_per_project == 3.0


def test_max_cost_premium_mode() -> None:
    """Premium mode caps the project at $8.00."""
    assert Settings(video_mode="premium").max_cost_usd_per_project == 8.0


def test_max_cost_default_is_standard() -> None:
    """The default video_mode is standard → $3.00 cap."""
    assert Settings().video_mode == "standard"
    assert Settings().max_cost_usd_per_project == 3.0


def test_committed_config_toml_loads_into_settings() -> None:
    """The real committed config.toml is readable by from_files (regression)."""
    repo_root = Path(__file__).resolve().parents[2]
    s = Settings.from_files(config_path=repo_root / "config.toml")
    # The committed [cost] caps round-trip.
    assert s.max_usd_standard == 3.0
    assert s.max_usd_premium == 8.0
    assert s.video_mode == "standard"  # committed [video].default_mode
