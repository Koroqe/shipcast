"""Fixed catalog of marketing hook templates (Slice 11).

PURE MODULE — stdlib only. No external API calls, no client construction, no
network. ``s04_plan``'s ``planner`` sub-agent selects ONE template key per
channel (``x`` / ``linkedin`` / ``blog``) and bakes the choice into the
``MarketingBrief``; later stages (``s09_graphics`` carousel, ``s10_copy``)
call :func:`render` to turn the chosen key + the picked changelog entry into a
concrete opening line, and assert the produced copy opens with it.

The seven templates are the v1 catalog from the PRD "Hook templates" table
(and the reference plan). The keys are FROZEN — they are the value space of
``MarketingBrief.hook_template_per_channel`` (validated against :data:`KEYS`).

| key                 | when                          | shape (example)                          |
|---------------------|-------------------------------|------------------------------------------|
| ``we_just_shipped`` | default for any feature ship  | "We just shipped X."                     |
| ``before_after``    | UX or speed improvement       | "Yesterday: the slow way. Today: X."     |
| ``problem_aha``     | bug fix / pain-point relief   | "If you've ever lost time to <pain>, …"  |
| ``numbered_list``   | multi-change releases         | "3 things we built: X."                  |
| ``behind_the_scenes`` | architectural / refactor    | "Why we rebuilt X."                      |
| ``5_sec_value``     | small high-leverage change    | "X now does Y in one click."             |
| ``social_proof``    | user-requested feature        | "You asked. We built X."                 |

Each renderer takes the entry mapping (``name`` / ``summary`` / ``details`` —
the :class:`~shipcast.schemas.ChangelogEntry` shape) and returns a single
non-empty opening line. Renderers are deterministic and tolerate missing
fields (falling back to the entry ``name``, then a generic phrase) so that a
sparse entry never yields an empty hook.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

# --------------------------------------------------------------------------- #
# Entry-field helpers
# --------------------------------------------------------------------------- #


def _field(entry: Mapping[str, object], key: str) -> str:
    """Return ``entry[key]`` as a stripped string, or ``""`` when absent/empty."""
    value = entry.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _name(entry: Mapping[str, object]) -> str:
    """The entry's headline name, falling back to a safe generic noun phrase."""
    return _field(entry, "name") or "this update"


def _summary(entry: Mapping[str, object]) -> str:
    """The entry's one-line summary, falling back to its name."""
    return _field(entry, "summary") or _name(entry)


# --------------------------------------------------------------------------- #
# Per-template renderers (each returns one concrete, non-empty opening line)
# --------------------------------------------------------------------------- #


def _we_just_shipped(entry: Mapping[str, object]) -> str:
    return f"We just shipped {_name(entry)}."


def _before_after(entry: Mapping[str, object]) -> str:
    return f"Before: the slow way. After: {_summary(entry)}"


def _problem_aha(entry: Mapping[str, object]) -> str:
    return (
        "If you've ever lost time to this, "
        f"{_name(entry)} fixes it: {_summary(entry)}"
    )


def _numbered_list(entry: Mapping[str, object]) -> str:
    return f"Here's what we built this week, starting with {_name(entry)}:"


def _behind_the_scenes(entry: Mapping[str, object]) -> str:
    return f"Behind the scenes: why we built {_name(entry)}."


def _5_sec_value(entry: Mapping[str, object]) -> str:
    return f"{_name(entry)}, in 5 seconds: {_summary(entry)}"


def _social_proof(entry: Mapping[str, object]) -> str:
    return f"You asked. We built {_name(entry)}."


# --------------------------------------------------------------------------- #
# The frozen catalog
# --------------------------------------------------------------------------- #

#: The seven fixed hook templates: key → renderer. FROZEN value space for
#: ``MarketingBrief.hook_template_per_channel``.
CATALOG: dict[str, Callable[[Mapping[str, object]], str]] = {
    "we_just_shipped": _we_just_shipped,
    "before_after": _before_after,
    "problem_aha": _problem_aha,
    "numbered_list": _numbered_list,
    "behind_the_scenes": _behind_the_scenes,
    "5_sec_value": _5_sec_value,
    "social_proof": _social_proof,
}

#: Tuple of the catalog keys (insertion order). The schema validates each
#: ``hook_template_per_channel`` value against this set.
KEYS: tuple[str, ...] = tuple(CATALOG.keys())


def render(key: str, entry: Mapping[str, object]) -> str:
    """Render hook template ``key`` for ``entry`` into a concrete opening line.

    Args:
        key: one of the seven catalog keys (:data:`KEYS`).
        entry: the picked changelog entry mapping (``name`` / ``summary`` /
            ``details``). Missing fields degrade gracefully to the name.

    Returns:
        A single, non-empty opening line.

    Raises:
        KeyError: ``key`` is not in the catalog (a programming error — the
            schema guarantees brief values are valid keys, so this fires only
            for direct misuse).
    """
    renderer = CATALOG[key]
    return renderer(entry)


__all__ = ["CATALOG", "KEYS", "render"]
