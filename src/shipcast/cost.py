"""Cost ledger + per-tool unit-cost constants (Slice 2).

This is a PURE leaf module: it imports nothing from `cli`, `clients`, or
`stage`. It depends on the `manifest` module for the `Manifest`/`StageRecord`
shape only (type-level), and otherwise operates on plain numbers, so the
dispatcher can use it without creating an import cycle.

The cost discipline (architecture.md → "Cost discipline"):

* `Settings.max_cost_usd_per_project` is the mode-dependent cap ($3 standard,
  $8 premium).
* BEFORE invoking any stage whose `run()` calls a paid API, the dispatcher
  computes ``projected = accumulated + next_unit_cost`` and aborts with
  ``CostCapExceeded`` when ``projected > cap`` (STRICT `>` — exactly-at-cap is
  allowed).
* Per-tool unit-cost constants are centralized here and mirror the honest
  published prices recorded in ``config.toml`` `[cost]`.

Security invariants (Slice 2 is security pre-review flagged):

* Accumulation is MONOTONIC and cannot underflow: a negative recorded
  ``cost_usd`` is treated as a corrupt manifest and raises ``ValueError``;
  ``projected`` rejects a negative ``next_unit_cost`` for the same reason.
* The ledger NEVER mutates the manifest — ``projected``/``would_exceed`` are
  pure reads, so the cap check has no side effects before the paid call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shipcast.manifest import Manifest

# --------------------------------------------------------------------------- #
# Per-tool unit-cost constants (USD). Honest published prices — see config.toml
# `[cost]`. These are the single source of truth the dispatcher charges against.
# --------------------------------------------------------------------------- #

#: Veo 3 Fast — one 8-second clip WITH audio.
VEO_FAST_CLIP_USD: float = 3.20
#: Gemini Imagen — one generated still image.
IMAGEN_IMAGE_USD: float = 0.04
#: Gemini multimodal — one narrative/analysis call.
GEMINI_MULTIMODAL_CALL_USD: float = 0.01
#: ElevenLabs narration — per minute of synthesized speech.
ELEVENLABS_PER_MINUTE_USD: float = 0.30

#: The metric key under which each stage records its incurred cost.
COST_METRIC_KEY: str = "cost_usd"


def _stage_cost(value: object) -> float:
    """Coerce a stored ``metrics.cost_usd`` into a non-negative float.

    None / missing → 0.0. A negative value means the manifest was tampered with
    or a stage wrote a bad metric; accumulation must never underflow, so we
    refuse it loudly rather than silently subtracting.
    """
    if value is None:
        return 0.0
    cost = float(value)  # type: ignore[arg-type]
    if cost < 0.0:
        raise ValueError(
            f"manifest carries a negative {COST_METRIC_KEY}={cost!r}; "
            "cost accumulation must be monotonic and cannot underflow"
        )
    return cost


def accumulated_cost_usd(manifest: Manifest) -> float:
    """Sum ``stages[*].metrics.cost_usd`` across every stage in the manifest.

    Stages without a recorded cost contribute 0. Raises ``ValueError`` if any
    stage carries a negative cost (corruption guard).
    """
    total = 0.0
    for record in manifest.stages.values():
        total += _stage_cost(record.metrics.get(COST_METRIC_KEY))
    return total


class CostLedger:
    """Pure read-only view over a manifest's accumulated cost.

    Constructed per dispatch from the live manifest. Never mutates it — the
    dispatcher records new cost via the normal manifest transition AFTER the
    paid call succeeds, not through this ledger.
    """

    def __init__(self, manifest: Manifest) -> None:
        self._manifest = manifest

    def accumulated(self) -> float:
        """Total cost already recorded across all stages."""
        return accumulated_cost_usd(self._manifest)

    def projected(self, next_unit_cost: float) -> float:
        """Return ``accumulated + next_unit_cost``.

        Rejects a negative ``next_unit_cost`` — paid calls only ever add cost.
        """
        if next_unit_cost < 0.0:
            raise ValueError(
                f"next_unit_cost={next_unit_cost!r} is negative; "
                "paid calls only ever add to the accumulated total"
            )
        return self.accumulated() + next_unit_cost

    def would_exceed(self, next_unit_cost: float, *, cap: float) -> bool:
        """True iff charging ``next_unit_cost`` would push the total OVER ``cap``.

        Uses STRICT greater-than: a projected total exactly equal to ``cap`` is
        allowed (UC-34-EC1 / TC-17.5 boundary).
        """
        return self.projected(next_unit_cost) > cap
