# Architecture Rules

## Layering

- `src/shipcast/cli.py` — Typer dispatcher; the ONLY place that mutates the manifest, holds the project lock, and renders the Review Checklist. Stages do not write manifests.
- `src/shipcast/project.py` — filesystem + manifest container; methods to create/load a project and resolve stage paths.
- `src/shipcast/manifest.py` — pure data + state transitions + hash helpers. No I/O outside of `load`/`save`.
- `src/shipcast/stage.py` + `src/shipcast/stages/` — Stage Protocol and concrete stage classes. Stages are pure functions of their inputs.
- `src/shipcast/clients/` — thin wrappers around external APIs (Anthropic, ElevenLabs, Gemini, Veo, Playwright, ffmpeg). Constructed lazily inside `stage.run()`. API-key validation in `__init__`.
- `src/shipcast/schemas.py` — Pydantic v2 models for stage artifacts (InputYaml, ChangelogEntry, EnrichedContext, BrandProposal, MarketingBrief, Storyboard, VideoBeats, CopyBundle, PackageManifest).
- `src/shipcast/marketing/` — hook catalog, code-screenshot renderer, LinkedIn carousel composer. Pure utilities, no external API calls.
- `src/shipcast/composition/` — PIL helpers (caption chips, text-outline, layout grid). Pure utilities.
- `src/shipcast/brand/` — brand pack loader and validator.
- `src/shipcast/changelog/` — markdown parser for `CHANGELOG.md` entries.

No layer reaches "upward". Stages do not import from `cli`. Clients do not import from `manifest`. Schemas do not import from anywhere else under `shipcast.*`.

## Stage Protocol contract

```python
class Stage(Protocol):
    id: str                          # e.g. "02_enrich"
    requires: tuple[str, ...]        # immediate upstream stage ids
    review_checklist_items: tuple[str, ...]
    output_schema: type[BaseModel]

    def check_inputs(self, project: Project) -> None: ...
    def run(self, project: Project) -> StageResult: ...
    def validate_outputs(self, project: Project, result: StageResult) -> None: ...
```

`BaseStage` provides default `check_inputs` (verifies every id in `requires` is `done` and has `human_approved_at`, plus the declared upstream artifact files exist) and `validate_outputs` (runs Pydantic validation). `BaseStage.additional_input_paths(project)` is an optional hook (default empty) for stages that consume operator-placed files outside the upstream stage's `outputs` (e.g. `s03_brand`'s `_brand/<slug>/` pack).

`BaseStage.pre_run_hook` is a class-level no-op test seam — production code MUST NOT read environment variables to alter stage behavior.

## Manifest is single source of truth

- One `manifest.json` per project. No sidecar JSONs.
- Atomic write: serialize to `manifest.json.tmp` → `fsync` → `os.replace()`.
- Deterministic serialization: `json.dumps(..., sort_keys=True, indent=2, ensure_ascii=False, separators=(",", ": "))` plus trailing newline.
- `config_snapshot` is written once on `Project.create` and is immutable once any stage leaves `pending` (raises `ConfigSnapshotLocked`).
- **Brand data does NOT live in `config_snapshot`.** Brand bytes live as files in `03_brand/`; downstream stages read from those files; `inputs_hash` covers drift. This is by design — a "brand_lock" stage that wrote into `config_snapshot` would violate the lock invariant.
- Schema version: `schema_version: 1`. Mismatched versions raise `ManifestMigrationNeeded`.

## Two-hash-family asymmetry (load-bearing)

- `compute_inputs_hash(paths)` — fast SHA-256 over sorted `(rel_path, mtime_ns, size_bytes)` tuples. Used ONLY for `inputs_hash`. False positives (re-run) cheap; false negatives only matter if mtime+size collide.
- `compute_outputs_hash(paths)` — byte-content SHA-256 over sorted `(rel_path, sha256_of_file_bytes)` tuples. Used for `outputs_hash_at_done` and `shipcast approve`'s recomputation. Detects ANY manual edit reliably, including no-op-mtime-bumps and same-size-different-bytes swaps.
- Do NOT swap which function is used where.

## Legal manifest state transitions

```
pending → running
running → done | failed | needs_review
done → pending     (via shipcast reset)
failed → running   (retry)
failed → pending   (via shipcast reset)
needs_review → running
```

Approval (`human_approved_at`) is orthogonal to status and meaningful only when status is `done`.

## Human gate

- Every stage finishes in `done`. Downstream stages refuse to run until the operator runs `shipcast approve <slug> <stage_id>`.
- `shipcast <verb> --rerun` and `shipcast reset` against a stage with downstream `human_approved_at` non-null refuse without `--yes`. The dispatcher lists every approval that will be discarded before proceeding.
- `shipcast approve` recomputes `compute_outputs_hash`; if different from stored `outputs_hash_at_done`, records `manually_edited=true` and lists the changed files. This is the mechanism the operator uses to edit `03_brand/proposal.json` between run and approve.

## Concurrency

- macOS / Linux only. Other platforms raise `UnsupportedPlatform` at CLI startup.
- `fcntl.flock` on `<project>/.lock` (exclusive, non-blocking).
- Clean-exit path unlinks the `.lock` file in `finally`. Crash exits leave the file (kernel releases the advisory lock anyway).
- `--no-lock` requires `SHIPCAST_NO_LOCK_ACK=1` env var. Bypass without ack raises `LockBypassNotAcknowledged`.

## Cost discipline

Slice 2 owns the cost ledger:
- `Settings.max_cost_usd_per_project` is mode-dependent ($3 standard, $8 premium).
- Dispatcher checks accumulated `manifest.stages[*].metrics.cost_usd` BEFORE invoking any stage that calls a paid API (Veo, Gemini Imagen, Gemini multimodal, ElevenLabs).
- Aborts with `CostCapExceeded` if the next call would exceed cap.
- Per-tool unit-cost constants are centralized in `src/shipcast/cost.py`.

## Determinism for idempotency tests

A stage rerun on identical inputs MUST produce byte-identical output files for the deterministic stages (parsing, schema validation, composition). LLM/AI stages (script, copy, image, video) need not be byte-deterministic, but non-determinism MUST be confined to the `run()` call (no `datetime.now()` inside the artifact body, no random `id` fields in the artifact JSON).
