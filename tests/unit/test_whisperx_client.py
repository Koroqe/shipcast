"""Unit tests for WhisperXClient — full 0% → coverage.

All heavy imports (whisper, torch) are mocked via monkeypatch; no real model
is loaded. Exercises:
- Happy path: two-segment transcription → list[WordTimestamp]
- Leading whitespace stripping (openai-whisper inserts leading space)
- Empty-word skipping
- NaN / None probability → confidence=None
- Float probability clamped to [0, 1]
- TypeError in float(probability) → confidence=None
- Zero-word result → ValueError with "WhisperX returned 0 words"
- ModuleNotFoundError when whisper is not installed
- __repr__ shape
- Constructor is always cheap (no args)
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from shipcast.clients.whisperx_client import WhisperXClient
from shipcast.schemas import WordTimestamp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_segment(words: list[dict[str, Any]]) -> dict[str, Any]:
    return {"words": words}


def _make_word(
    word: str,
    start: float = 0.0,
    end: float = 1.0,
    probability: Any = 0.9,
) -> dict[str, Any]:
    return {"word": word, "start": start, "end": end, "probability": probability}


def _fake_whisper_module(segments: list[dict[str, Any]]) -> MagicMock:
    """Return a mock `whisper` module whose model.transcribe returns the given segments."""
    mock_model = MagicMock()
    mock_model.transcribe.return_value = {"segments": segments}

    mock_whisper = MagicMock()
    mock_whisper.load_model.return_value = mock_model
    return mock_whisper


# ---------------------------------------------------------------------------
# TC: constructor is cheap (no args, no heavy imports)
# ---------------------------------------------------------------------------


def test_constructor_requires_no_args() -> None:
    client = WhisperXClient()
    assert isinstance(client, WhisperXClient)


def test_repr_contains_class_name() -> None:
    client = WhisperXClient()
    assert "WhisperXClient" in repr(client)


# ---------------------------------------------------------------------------
# TC: happy path — two segments, multiple words each
# ---------------------------------------------------------------------------


def test_happy_path_two_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [
        _make_segment([
            _make_word("Hello", 0.0, 0.5, 0.95),
            _make_word(" world", 0.5, 1.0, 0.88),
        ]),
        _make_segment([
            _make_word(" foo", 1.1, 1.5, 0.72),
        ]),
    ]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    result = client.transcribe_with_alignment(
        mp3_path=__file__,  # type: ignore[arg-type]  # Path-like string is fine for the mock
        model_name="base.en",
    )

    assert len(result) == 3
    assert result[0].word == "Hello"
    assert result[1].word == "world"   # leading space stripped
    assert result[2].word == "foo"     # leading space stripped
    assert all(isinstance(w, WordTimestamp) for w in result)


# ---------------------------------------------------------------------------
# TC: leading whitespace stripped, empty word skipped
# ---------------------------------------------------------------------------


def test_empty_word_after_strip_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [
        _make_segment([
            _make_word("  ", 0.0, 0.1, 0.5),   # strips to empty → skip
            _make_word("hello", 0.1, 0.5, 0.9),
        ]),
    ]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    result = client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]
    assert len(result) == 1
    assert result[0].word == "hello"


# ---------------------------------------------------------------------------
# TC: None probability → confidence=None
# ---------------------------------------------------------------------------


def test_none_probability_gives_none_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [_make_segment([_make_word("hi", 0.0, 1.0, probability=None)])]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    result = client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]
    assert result[0].confidence is None


# ---------------------------------------------------------------------------
# TC: NaN probability → confidence=None
# ---------------------------------------------------------------------------


def test_nan_probability_gives_none_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [_make_segment([_make_word("hi", 0.0, 1.0, probability=float("nan"))])]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    result = client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]
    assert result[0].confidence is None


# ---------------------------------------------------------------------------
# TC: probability clamped to [0, 1]
# ---------------------------------------------------------------------------


def test_probability_above_1_clamped_to_1(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [_make_segment([_make_word("hi", 0.0, 1.0, probability=1.5)])]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    result = client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]
    assert result[0].confidence == 1.0


def test_probability_below_0_clamped_to_0(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [_make_segment([_make_word("hi", 0.0, 1.0, probability=-0.1)])]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    result = client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]
    assert result[0].confidence == 0.0


# ---------------------------------------------------------------------------
# TC: TypeError/ValueError from float(probability) → confidence=None
# ---------------------------------------------------------------------------


def test_unconvertible_probability_gives_none_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An object that raises TypeError when float() is called on it
    class BadFloat:
        def __float__(self) -> float:
            raise TypeError("not a number")

    segments = [_make_segment([_make_word("hi", 0.0, 1.0, probability=BadFloat())])]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    result = client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]
    assert result[0].confidence is None


# ---------------------------------------------------------------------------
# TC: zero-word result → ValueError (FR-5.9)
# ---------------------------------------------------------------------------


def test_zero_words_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty segments list
    mock_whisper = _fake_whisper_module([])
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    with pytest.raises(ValueError, match="WhisperX returned 0 words"):
        client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]


def test_segments_with_no_words_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Segments present but no words entries
    segments = [{"words": []}, {"words": []}]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    with pytest.raises(ValueError, match="WhisperX returned 0 words"):
        client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]


def test_only_whitespace_words_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # All words strip to empty
    segments = [_make_segment([_make_word("   ", 0.0, 1.0)])]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    with pytest.raises(ValueError, match="WhisperX returned 0 words"):
        client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TC: whisper not installed → ModuleNotFoundError propagates
# ---------------------------------------------------------------------------


def test_module_not_found_when_whisper_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `whisper` is not importable, ModuleNotFoundError propagates unchanged."""
    # Remove whisper from sys.modules so the lazy import inside the method fails
    monkeypatch.delitem(sys.modules, "whisper", raising=False)

    # Patch builtins.__import__ to raise ModuleNotFoundError for "whisper"
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__  # type: ignore[union-attr]

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "whisper":
            raise ModuleNotFoundError("No module named 'whisper'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)

    client = WhisperXClient()
    with pytest.raises(ModuleNotFoundError):
        client.transcribe_with_alignment(__file__, model_name="base.en")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TC: device keyword-argument is forwarded to load_model
# ---------------------------------------------------------------------------


def test_device_kwarg_forwarded_to_load_model(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [_make_segment([_make_word("ok", 0.0, 1.0, 0.8)])]
    mock_whisper = _fake_whisper_module(segments)
    monkeypatch.setitem(sys.modules, "whisper", mock_whisper)

    client = WhisperXClient()
    client.transcribe_with_alignment(__file__, model_name="small.en", device="mps")  # type: ignore[arg-type]

    mock_whisper.load_model.assert_called_once_with("small.en", device="mps")
