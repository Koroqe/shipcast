"""Schema tests for the ``StoryboardBeat.show_interface`` flag.

``show_interface`` lets the storyboard sub-agent mark beats that DEPICT the
product's UI so ``s06_video_assets`` can ground their Imagen stills in the real
app screenshot. It defaults to ``False`` so existing data and the brief's
``video_beats`` skeleton (which never set it) stay valid.
"""

from __future__ import annotations

from shipcast.schemas import StoryboardBeat


def test_show_interface_defaults_false_when_omitted() -> None:
    """A beat without ``show_interface`` validates and defaults to False."""
    beat = StoryboardBeat.model_validate(
        {"image_prompt": "a UI shot", "narration": "look at this", "duration_sec": 4.0}
    )
    assert beat.show_interface is False


def test_show_interface_true_is_accepted() -> None:
    """``show_interface=True`` round-trips through the model."""
    beat = StoryboardBeat.model_validate(
        {
            "image_prompt": "a UI shot",
            "narration": "look at this",
            "duration_sec": 4.0,
            "show_interface": True,
        }
    )
    assert beat.show_interface is True


def test_show_interface_false_is_accepted() -> None:
    """``show_interface=False`` is explicitly accepted."""
    beat = StoryboardBeat(
        image_prompt="abstract hero",
        narration="ship it",
        duration_sec=3.5,
        show_interface=False,
    )
    assert beat.show_interface is False
    # Serialises into the JSON dump (so s06 reads it off disk).
    assert beat.model_dump(mode="json")["show_interface"] is False
