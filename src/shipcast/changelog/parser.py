"""Hand-rolled scanner for ``CHANGELOG.md`` in the canonical changelog format.

This is deliberately NOT a full markdown library. It recognises exactly the
structure documented in ``~/.claude/rules/changelog.md``:

    ## YYYY-MM-DD                  <- day heading (newest first)
    ### <name> — HH:MM UTC         <- entry heading (em dash + time + "UTC")
    **Summary:** <one-liner>
    **Details:** <fuller text>

A day heading sets the *current date*; every subsequent entry heading inherits
it until the next day heading. The em dash is U+2014 (``—``); the ``— HH:MM UTC``
time suffix is optional (``time_utc`` is then ``None``).

Determinism: ``parse_changelog`` is a pure function of the file bytes — no
clock, no randomness — so re-parsing identical content yields equal
:class:`~shipcast.schemas.ChangelogEntry` objects and byte-identical JSON.
"""

from __future__ import annotations

import re
from pathlib import Path

from shipcast.errors import ChangelogFileMissing
from shipcast.schemas import ChangelogEntry

# A day heading: "## 2026-06-02" (exactly an ISO date after the marker).
_DAY_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")

# An entry heading: "### <name>" with an optional " — HH:MM UTC" suffix.
# The separator is an em dash (U+2014). The time + "UTC" suffix is optional.
_ENTRY_RE = re.compile(
    r"^###\s+(?P<name>.*?)"
    r"(?:\s+—\s+(?P<time>\d{1,2}:\d{2})\s+UTC)?"
    r"\s*$"
)

# Field lines inside an entry body.
_SUMMARY_RE = re.compile(r"^\*\*Summary:\*\*\s*(.*)$")
_DETAILS_RE = re.compile(r"^\*\*Details:\*\*\s*(.*)$")


def parse_changelog(path: Path) -> list[ChangelogEntry]:
    """Parse ``path`` into a list of :class:`ChangelogEntry`, document order.

    Entries are returned in the order they appear in the file (newest day first,
    newest entry first within a day, per the changelog rule). An empty file or a
    file with no ``## YYYY-MM-DD`` day headings yields ``[]``.

    Raises:
        ChangelogFileMissing: if ``path`` does not exist. The file is NEVER
            created as a side effect.
    """
    if not path.is_file():
        raise ChangelogFileMissing(
            f"CHANGELOG.md not found at {path} — shipcast never auto-creates it."
        )

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    entries: list[ChangelogEntry] = []
    current_date: str | None = None

    # State for the entry currently being accumulated.
    pending_name: str | None = None
    pending_time: str | None = None
    pending_summary = ""
    pending_details = ""
    pending_raw: list[str] = []

    def _flush() -> None:
        nonlocal pending_name, pending_time, pending_summary
        nonlocal pending_details, pending_raw
        if pending_name is None or current_date is None:
            # An entry heading seen before any day heading is ignored (the rule
            # requires every entry to sit under a date).
            pending_name = None
            pending_time = None
            pending_summary = ""
            pending_details = ""
            pending_raw = []
            return
        entries.append(
            ChangelogEntry(
                name=pending_name,
                date=current_date,
                time_utc=pending_time,
                summary=pending_summary,
                details=pending_details,
                raw="\n".join(pending_raw).rstrip(),
            )
        )
        pending_name = None
        pending_time = None
        pending_summary = ""
        pending_details = ""
        pending_raw = []

    for line in lines:
        day_match = _DAY_RE.match(line)
        if day_match:
            _flush()
            current_date = day_match.group(1)
            continue

        entry_match = _ENTRY_RE.match(line)
        if entry_match and line.lstrip().startswith("###"):
            _flush()
            pending_name = entry_match.group("name").strip()
            pending_time = entry_match.group("time")
            pending_raw = [line]
            continue

        if pending_name is not None:
            pending_raw.append(line)
            summary_match = _SUMMARY_RE.match(line)
            if summary_match:
                pending_summary = summary_match.group(1).strip()
                continue
            details_match = _DETAILS_RE.match(line)
            if details_match:
                pending_details = details_match.group(1).strip()
                continue

    _flush()
    return entries


def find_entry(
    entries: list[ChangelogEntry], heading: str
) -> ChangelogEntry | None:
    """Return the entry whose name matches ``heading`` (trimmed, case-insensitive).

    The comparison strips surrounding whitespace and lowercases both sides, so
    ``"  add csv export  "`` matches an entry named ``"Add CSV Export"``. Returns
    the first matching entry, or ``None`` when nothing matches.
    """
    needle = heading.strip().casefold()
    for entry in entries:
        if entry.name.strip().casefold() == needle:
            return entry
    return None
