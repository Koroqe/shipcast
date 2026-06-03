"""Unit tests for the marketing hook-template catalog (Slice 11).

Owned TCs:
- TC-7.2 / AC-6.2 / AC-14.1: each of the 7 catalog templates renders a
  non-empty string given a sample entry; no exception.

The catalog is a pure module (``src/shipcast/marketing/hooks.py``) with NO
external API calls — these tests neither mock nor touch any client.
"""

from __future__ import annotations

import pytest

from shipcast.marketing import hooks

#: The seven fixed catalog keys (PRD "Hook templates" table; reference plan).
_EXPECTED_KEYS = (
    "we_just_shipped",
    "before_after",
    "problem_aha",
    "numbered_list",
    "behind_the_scenes",
    "5_sec_value",
    "social_proof",
)

_SAMPLE_ENTRY: dict[str, object] = {
    "name": "Add CSV export",
    "summary": "Users can now download their report as a spreadsheet file.",
    "details": (
        "Adds a GET /api/reports/:id/export endpoint that streams report rows "
        "as CSV. Auth-protected, validates ownership, paginates large datasets."
    ),
}


def test_catalog_has_exactly_seven_keys() -> None:
    """The catalog exposes exactly the 7 documented templates, no more, no fewer."""
    assert set(hooks.CATALOG.keys()) == set(_EXPECTED_KEYS)
    assert len(hooks.CATALOG) == 7
    # The public KEYS tuple mirrors the catalog and is used by the schema.
    assert set(hooks.KEYS) == set(_EXPECTED_KEYS)


def test_schema_key_tuple_matches_catalog() -> None:
    """Drift guard: the leaf-schema's frozen key tuple equals the catalog keys.

    ``schemas.HOOK_TEMPLATE_KEYS`` is duplicated to keep ``schemas`` a leaf
    module; this asserts the duplicate never drifts from ``hooks.KEYS``.
    """
    from shipcast.schemas import HOOK_TEMPLATE_KEYS

    assert set(HOOK_TEMPLATE_KEYS) == set(hooks.KEYS)


@pytest.mark.parametrize("key", _EXPECTED_KEYS)
def test_tc_7_2_render_returns_non_empty_string(key: str) -> None:
    """TC-7.2: every template renders a non-empty string for a sample entry."""
    out = hooks.render(key, _SAMPLE_ENTRY)
    assert isinstance(out, str)
    assert out.strip() != ""


def test_render_incorporates_entry_name() -> None:
    """The rendered line is concrete — at minimum the default template uses the name."""
    out = hooks.render("we_just_shipped", _SAMPLE_ENTRY)
    assert "Add CSV export" in out


def test_render_unknown_key_raises_key_error() -> None:
    """An unknown template key is a programming error → KeyError (not silent)."""
    with pytest.raises(KeyError):
        hooks.render("not_a_real_template", _SAMPLE_ENTRY)


def test_render_tolerates_missing_entry_fields() -> None:
    """A sparse entry (only name) still renders non-empty for every template."""
    sparse: dict[str, object] = {"name": "Fix login loop"}
    for key in _EXPECTED_KEYS:
        out = hooks.render(key, sparse)
        assert out.strip() != ""
