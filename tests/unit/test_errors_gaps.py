"""Tests for uncovered lines in errors.py (lines 441-446, 471-475).

Lines 441-446: PlaywrightUploadFailed.__init__ — message truncation to first line, max 200 chars.
Lines 471-475: DurationOutOfTolerance.__init__ — named attributes + message format.
"""

from __future__ import annotations

import pytest

from shipcast.errors import DurationOutOfTolerance, PlaywrightUploadFailed

# ---------------------------------------------------------------------------
# PlaywrightUploadFailed (lines 441-446)
# ---------------------------------------------------------------------------


def test_playwright_upload_failed_basic() -> None:
    original = ValueError("click failed on button")
    err = PlaywrightUploadFailed("click_publish", original)
    assert err.step_name == "click_publish"
    assert err.original is original
    assert err.partial_upload_state == "none"
    assert "click_publish" in str(err)
    assert "ValueError" in str(err)


def test_playwright_upload_failed_partial_upload_state() -> None:
    original = RuntimeError("network error")
    err = PlaywrightUploadFailed(
        "fill_title", original, partial_upload_state="draft_created"
    )
    assert err.partial_upload_state == "draft_created"


def test_playwright_upload_failed_truncates_to_first_line() -> None:
    """Multi-line exception message is truncated to the first line."""
    original = ValueError("first line\nsecond line\nthird line")
    err = PlaywrightUploadFailed("step", original)
    # The message should not contain the second or third lines
    assert "second line" not in str(err)
    assert "third line" not in str(err)
    assert "first line" in str(err)


def test_playwright_upload_failed_caps_at_200_chars() -> None:
    """First line is capped at 200 characters."""
    long_message = "x" * 300
    original = ValueError(long_message)
    err = PlaywrightUploadFailed("step", original)
    # The first-line truncation at 200 chars means the message won't have 300 x's
    assert long_message not in str(err)
    # But it should have a 200-char prefix
    assert "x" * 200 in str(err)


# ---------------------------------------------------------------------------
# DurationOutOfTolerance (lines 471-475)
# ---------------------------------------------------------------------------


def test_duration_out_of_tolerance_named_attributes() -> None:
    err = DurationOutOfTolerance(actual=12.3, target=15.0, delta=-2.7, tolerance=1.0)
    assert err.actual == pytest.approx(12.3)
    assert err.target == pytest.approx(15.0)
    assert err.delta == pytest.approx(-2.7)
    assert err.tolerance == pytest.approx(1.0)


def test_duration_out_of_tolerance_message_contains_values() -> None:
    err = DurationOutOfTolerance(actual=12.3, target=15.0, delta=-2.7, tolerance=1.0)
    msg = str(err)
    assert "12.30" in msg
    assert "15.00" in msg
    assert "1.00" in msg


def test_duration_out_of_tolerance_message_contains_remediation() -> None:
    err = DurationOutOfTolerance(actual=5.0, target=10.0, delta=-5.0, tolerance=2.0)
    msg = str(err)
    assert "rerun" in msg.lower() or "Remediation" in msg


def test_duration_out_of_tolerance_is_shipcast_error() -> None:
    from shipcast.errors import ShipcastError

    err = DurationOutOfTolerance(actual=1.0, target=2.0, delta=-1.0, tolerance=0.5)
    assert isinstance(err, ShipcastError)
