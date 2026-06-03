"""Unit tests for ``shipcast.changelog.parser`` (Slice 5).

Covers TC-4.1 .. TC-4.6, TC-4.11, TC-21.4. The parser reads the canonical
changelog format documented in ``~/.claude/rules/changelog.md``:

    ## YYYY-MM-DD               <- day heading (newest first)
    ### <name> — HH:MM UTC      <- entry heading
    **Summary:** ...
    **Details:** ...
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipcast.changelog.parser import find_entry, parse_changelog
from shipcast.errors import ChangelogFileMissing
from shipcast.schemas import ChangelogEntry

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "changelogs"


# --------------------------------------------------------------------------- #
# TC-4.1 — canonical format
# --------------------------------------------------------------------------- #


def test_canonical_single_entry() -> None:
    entries = parse_changelog(FIXTURES / "canonical.md")

    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, ChangelogEntry)
    assert entry.name == "Add CSV export"
    assert entry.date == "2026-06-02"
    assert entry.time_utc == "14:30"
    assert entry.summary == (
        "Users can now download their report as a spreadsheet file."
    )
    assert "GET /api/reports/:id/export" in entry.details
    # The raw markdown of the entry is preserved.
    assert entry.raw.startswith("### Add CSV export — 14:30 UTC")
    assert "**Summary:**" in entry.raw


# --------------------------------------------------------------------------- #
# TC-4.2 — multiple entries per day
# --------------------------------------------------------------------------- #


def test_multiple_entries_per_day() -> None:
    entries = parse_changelog(FIXTURES / "multi_per_day.md")

    assert len(entries) == 2
    assert entries[0].name == "Add CSV export"
    assert entries[1].name == "Fix login redirect loop"
    # Both share the same date; names differ.
    assert entries[0].date == entries[1].date == "2026-06-02"
    assert entries[0].name != entries[1].name
    assert entries[0].time_utc == "14:30"
    assert entries[1].time_utc == "11:05"


# --------------------------------------------------------------------------- #
# TC-4.3 — missing time field
# --------------------------------------------------------------------------- #


def test_missing_time_yields_none() -> None:
    entries = parse_changelog(FIXTURES / "missing_time.md")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "Add dark mode"
    assert entry.time_utc is None
    assert entry.date == "2026-06-02"
    assert entry.summary == "The app now has a dark colour scheme."


# --------------------------------------------------------------------------- #
# TC-4.4 — empty file → empty list
# --------------------------------------------------------------------------- #


def test_empty_file_returns_empty_list(tmp_path: Path) -> None:
    empty = tmp_path / "CHANGELOG.md"
    empty.write_text("", encoding="utf-8")
    assert parse_changelog(empty) == []

    # The shipped empty fixture behaves the same.
    assert parse_changelog(FIXTURES / "empty.md") == []


# --------------------------------------------------------------------------- #
# TC-4.5 — no date headings → empty list
# --------------------------------------------------------------------------- #


def test_no_date_headings_returns_empty_list() -> None:
    assert parse_changelog(FIXTURES / "no_dates.md") == []


# --------------------------------------------------------------------------- #
# TC-4.6 — missing file raises, never auto-creates
# --------------------------------------------------------------------------- #


def test_missing_file_raises_and_does_not_create(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist" / "CHANGELOG.md"

    with pytest.raises(ChangelogFileMissing):
        parse_changelog(missing)

    assert not missing.exists()
    assert not missing.parent.exists()


# --------------------------------------------------------------------------- #
# TC-4.11 — find_entry helper is trimmed + case-insensitive
# --------------------------------------------------------------------------- #


def test_find_entry_case_insensitive_and_trimmed() -> None:
    entries = parse_changelog(FIXTURES / "canonical.md")

    found = find_entry(entries, "  add csv export  ")
    assert found is not None
    assert found.name == "Add CSV export"


def test_find_entry_exact_match() -> None:
    entries = parse_changelog(FIXTURES / "multi_per_day.md")

    found = find_entry(entries, "Fix login redirect loop")
    assert found is not None
    assert found.name == "Fix login redirect loop"


def test_find_entry_missing_returns_none() -> None:
    entries = parse_changelog(FIXTURES / "canonical.md")
    assert find_entry(entries, "No such entry") is None


def test_find_entry_empty_list_returns_none() -> None:
    assert find_entry([], "anything") is None


# --------------------------------------------------------------------------- #
# TC-21.4 — determinism: same bytes → byte-identical JSON
# --------------------------------------------------------------------------- #


def test_determinism_byte_identical_json() -> None:
    first = parse_changelog(FIXTURES / "multi_per_day.md")
    second = parse_changelog(FIXTURES / "multi_per_day.md")

    dumped_first = json.dumps(
        [e.model_dump() for e in first], sort_keys=True, indent=2
    )
    dumped_second = json.dumps(
        [e.model_dump() for e in second], sort_keys=True, indent=2
    )
    assert dumped_first == dumped_second
    # And the objects themselves compare equal.
    assert first == second
