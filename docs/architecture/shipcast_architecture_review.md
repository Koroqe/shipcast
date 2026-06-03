# Architecture Review — shipcast

**Reviewer:** Software Architect
**Date:** 2026-06-03
**Scope reviewed:** `docs/PRD.md` (17 sections, 101 FRs), `docs/use-cases/shipcast_use_cases.md` (36 UCs), the binding reference plan `docs/qa/shipcast_implementation_plan.md` (24 slices), project invariants (`.claude/CLAUDE.md`, `.claude/rules/{architecture,testing,security}.md`), the on-disk scaffold (`src/shipcast/` skeleton, `pyproject.toml`, `config.toml`, `.env.example`, `projects/_template/`), and the proven upstream pipeline scaffold it reuses.

---

## Verdict: **PASS** (with conditions)

The planned architecture is sound and faithfully reuses a battle-hardened upstream pipeline scaffold. All hard invariants in `.claude/rules/architecture.md` and `security.md` are respected by the layout. The reuse/copy/extend claims in the reference plan were verified against the actual scaffold code and are realistic. No CRITICAL findings. Three MAJOR findings require correction during the relevant slices before those slices can be considered done; they are spec/wiring gaps, not structural-boundary violations. The conditions are enumerated in Action Items.

**REST conventions:** N/A. shipcast is a Typer CLI with no HTTP API and no database. There are no endpoints, no auth middleware, and no network surface other than outbound calls to external SaaS APIs (validated for SSRF on the Playwright `live_url` path only). This is noted explicitly so the absence of REST/API findings is not mistaken for an omission.

---

## Validated Invariants

- **Layering — cli is the only manifest mutator.** Confirmed in the upstream scaffold's `cli.py`: the dispatcher owns `transition`, `save_manifest`, `approve`, lock acquisition, `compute_outputs_hash`, and the Review Checklist. `manifest.py` exposes pure state-transition methods that return new `Manifest` copies; it performs no I/O except `load`/`save`. Satisfies `architecture.md` "Layering" and FR-1.20.
- **Layering — stages do not import cli; clients do not import manifest; schemas import nothing under the package.** The reused `stage.py`/`_base.py` import only `manifest` (for `StageStatus`/`compute_inputs_hash`) and `errors`; `gemini_client.py` imports only `config`/`errors`, never `manifest`. The planned `schemas.py` is a leaf Pydantic module. Satisfies `architecture.md` final layering paragraph.
- **Manifest single source of truth, no sidecars.** FR-1.4 and the layout in the plan write only `manifest.json`; per-stage artifacts are data files, not status files. Satisfies `architecture.md` "Manifest is single source of truth."
- **Atomic temp-rename writes.** `manifest.py:save` serializes to `<path>.tmp`, `os.fsync`, then `os.replace`. Satisfies FR-1.9 / NFR-16.7 / AC-1.13.
- **Deterministic serialization.** `manifest.py` uses `sort_keys=True, indent=2, ensure_ascii=False, separators=(",", ": ")` plus trailing newline. Satisfies FR-1.10.
- **Two-hash-family asymmetry used correctly.** `compute_inputs_hash` = `(rel_path, mtime_ns, size)` SHA-256, used only for `inputs_hash`; `compute_outputs_hash` = byte-content SHA-256, used for `outputs_hash_at_done` and `approve`'s edit detection. The plan and PRD (FR-1.13, FR-3.10) apply each in the correct place and never swap them. The brand-edit-detection flow (UC-4/UC-15) correctly depends on `compute_outputs_hash`, including the byte-identical-different-mtime edge case (UC-15-EC1). Satisfies `architecture.md` "Two-hash-family asymmetry."
- **schema_version + migration guard.** `manifest.py` carries `schema_version` and raises `ManifestMigrationNeeded` on mismatch. Satisfies FR-1.12.
- **config_snapshot immutability.** `manifest.py:update_config_snapshot` raises `ConfigSnapshotLocked` if any stage is not `pending`. Satisfies FR-1.11.
- **Brand-stage decision is sound.** Verified: collapsing `brand_extract` + `brand_lock` into a single `s03_brand` correctly avoids the `ConfigSnapshotLocked` violation. Brand bytes live as files under `03_brand/` (`proposal.json`, `logo.png`, `style_sheet.png`); downstream stages read those files; `inputs_hash` covers drift; NO write to `config_snapshot` on any `s03_brand` code path (FR-3.11, NFR-3.1, AC-3.5). The operator-edit-between-run-and-approve workflow reuses the existing `approve` + `compute_outputs_hash` mechanism unchanged. This is the correct resolution of the original CRITICAL the plan's first critic pass caught.
- **Legal manifest state transitions.** `manifest.py:transition` enforces the exact matrix in FR-1.8 and `architecture.md` via `is_legal_transition`; illegal moves raise `IllegalTransition`. `reset` clears all fields and walks transitive downstream via the `downstream_of` map. Satisfies FR-1.17/1.18 and the cascade-confirmation guard FR-1.19.
- **Stage Protocol contract.** `stage.py` defines the `Stage` Protocol with `id`, `requires`, `review_checklist_items`, `output_schema`, `check_inputs`, `run`, `validate_outputs`, `pre_run_hook`. `_base.py` provides default `check_inputs` (status==done + human_approved_at + upstream files exist), `validate_outputs`, `additional_input_paths` (default empty), and the path-traversal output guard `_validate_output_paths`. Satisfies FR-1.23–1.25 and `architecture.md` "Stage Protocol contract."
- **additional_input_paths hook for the brand pack.** The hook exists and is the correct seam for `s03_brand` to pull `_brand/<slug>/` files into its `inputs_hash`. Satisfies FR-1.25.
- **pre_run_hook is a no-op test seam, not an env-var branch.** `stage.py`/`_base.py` define `pre_run_hook` as a default no-op; production code does not read test env vars to alter behavior. `testing.md` reinforces this for the two-process race test. Satisfies FR-1.26.
- **Lazy client construction.** `cli.py` imports client classes lazily and never constructs them at startup; `gemini_client.__init__` raises `MissingApiKey("GEMINI_API_KEY")` when the `SecretStr` is empty, with the key NAME only. The plan keeps all client construction inside `stage.run()` via `clients_factory`. Satisfies `security.md` "Lazy client construction" and FR-1.29/NFR-16.10.
- **SecretStr never serialized into config_snapshot.** `config.py:public_dict()` programmatically excludes every `SecretStr`-annotated field via `_is_secret_annotation`, and is the only shape persisted into `config_snapshot`. A future-added secret is auto-excluded. Satisfies FR-1.29 / NFR-16.8 / `security.md` "Secrets handling." `.env.example` on disk contains bare key names with empty values, matching the security rule.
- **Cost ledger atomicity.** Plan Slice 2 + FR-1.27/1.28 put the cap check in the dispatcher BEFORE any paid call; the projected (accumulated + next-unit) total is compared to the mode cap, so the check is a pre-condition gate, not a post-hoc reconciliation. Per-tool unit costs are centralized in `cost.py`/`config.toml [cost]`. Because the dispatcher is the sole manifest mutator and `metrics.cost_usd` is only written after a successful paid call, accumulated cost is monotonic and cannot underflow. Satisfies `architecture.md` "Cost discipline" and UC-20.
- **URL validator precedes Playwright goto.** FR-3.6 / AC-3.6 / UC-4-E5 require the `InputYaml` `live_url` validator (https-only, reject RFC1918/loopback/link-local) to run before any `playwright_client` call. The plan sequences this correctly in `s03_brand.run()` step 1 and in `s02_enrich`. Satisfies `security.md` "Input validation" and the Slice 8 pre-review note.
- **shipcast never writes into the target repo.** The pipeline reads `CHANGELOG.md` and runs `gh`/`git log` in `repo_path` but writes only under `projects/<slug>/`. `repo_path` validation (under allowed root, no `..`, must contain `CHANGELOG.md`, never auto-create) is enforced in `InputYaml`. Output paths are confined to `stage_dir` by `_validate_output_paths`. Satisfies `security.md` "Filesystem."
- **Concurrency + platform.** `fcntl.flock(LOCK_EX|LOCK_NB)` on `<project>/.lock`; `--no-lock` honored only with the ack env var (`SHIPCAST_NO_LOCK_ACK=1`), else `LockBypassNotAcknowledged`; non-macOS/Linux raises `UnsupportedPlatform` at startup. The reused `locking.py`/`cli.py` implement exactly this (the sibling uses `FACTORY_NO_LOCK_ACK`; shipcast renames to `SHIPCAST_NO_LOCK_ACK`). Satisfies FR-1.30–1.32 / UC-21/22/23.
- **Slice sequencing / dependency ordering.** All 24 slices are sequential-within-wave, justified by shared edits to `cli.py` and `stages/__init__.py` (the registry). I traced the dependency chain: every slice's `Done when:` depends only on outputs of equal-or-earlier slices (e.g., Slice 10 `s03_brand` depends on Slices 8+9 clients; Slice 13 `s06_video_assets` depends on Slice 11 brief schema + Slice 9 gemini aspect + ffmpeg Ken-Burns; Slice 15 `s08_video` depends on Slices 13+14). No slice depends on a later slice. Satisfies the plan's wave model and the CLAUDE.md wave-ordering rule.
- **Determinism for idempotency.** Deterministic stages (`s01_pick` parse, schema validation, `s11_package` zip with sorted entries) are required to be byte-identical on re-run (FR-4.5, FR-13.3, NFR-16.6); LLM/AI stages confine non-determinism to `run()`. Consistent with `architecture.md` "Determinism for idempotency tests."

---

## Findings

### 1. [MAJOR] `voice.md` read-path is inconsistent: operator-supplied in `_brand/<slug>/` but read from `03_brand/voice.md` downstream — no FR copies it.
**Concern.** `voice.md` is defined as a REQUIRED operator-supplied brand-pack file living at `projects/_brand/<brand_slug>/voice.md` (PRD FR-3.1, FR-3.3; CLAUDE.md brand-pack contract). But FR-6.2, FR-10.2, FR-12.1, AC-10.5, AC-14.3 and `architecture.md` all read it from `03_brand/voice.md`. The `s03_brand` output contract (FR-3.8) writes exactly three artifacts — `proposal.json`, `logo.png`, `style_sheet.png` — and does **not** include `voice.md`. There is no FR that copies `_brand/<slug>/voice.md` into `03_brand/`. As written, downstream stages will read a path that never gets populated, and `s03_brand`'s `inputs_hash` (via `additional_input_paths`) — not its outputs — is what currently covers `voice.md` drift. This is a real wiring gap that will surface as a missing-file error in `s08_video` and `s10_copy`.
**Remediation (pick one, document in the chosen slice):** (a) Have `s03_brand.run()` copy `_brand/<slug>/voice.md` into `03_brand/voice.md` as a fourth declared output, so downstream reads and `compute_outputs_hash` edit-detection both work; OR (b) change FR-6.2/FR-10.2/FR-12.1/AC-10.5/AC-14.3 to read from `_brand/<slug>/voice.md` directly and make `voice.md` part of each consuming stage's `additional_input_paths` for drift coverage. Option (a) is preferred because it keeps all stage inputs reading from declared upstream-stage outputs (cleaner boundary) and makes operator edits to voice between brand-run and approve visible via `manually_edited`. This is a spec correction; resolve it during Slice 10 (`s03_brand`) before Slices 15/19 consume the path.

### 2. [MAJOR] `BaseStage.check_inputs` does not actually verify upstream `inputs_hash` still matches, contradicting FR-1.24 and `architecture.md`.
**Concern.** FR-1.24 states `check_inputs` verifies "the stored `inputs_hash` still matches," and `architecture.md` implies drift via `inputs_hash` triggers re-run prompts. The reused `_base.py:check_inputs` verifies status==done, `human_approved_at` non-null, and upstream output files exist — but it does **not** recompute and compare `inputs_hash`. The hash is computed and stored (`compute_stage_inputs_hash`) but never re-checked at downstream `check_inputs` time. This means brand drift (FR-3.11's stated mechanism) does not automatically block or warn a downstream stage that was already run against stale inputs; the operator would have to manually `--rerun`. The reused behavior is internally consistent and safe (it never runs against missing files), but the PRD over-claims the drift-detection guarantee.
**Remediation.** Either (a) implement the `inputs_hash` re-check in shipcast's `_base.check_inputs` (recompute current upstream `inputs_hash`, compare to the value stored at this stage's last run, and raise/warn on mismatch) to match FR-1.24 as written; OR (b) soften FR-3.11 and FR-1.24 to state that `inputs_hash` is recorded for audit and powers `--rerun` invalidation, not an automatic downstream block. Decide during Slice 1/4 (scaffold + gates) since it affects the reused base class; if (a), it is net-new code beyond verbatim reuse and must be called out in the slice. Recommend (b) unless the operator specifically wants auto-blocking — the human gate already forces a deliberate approve at every stage, so silent stale-input runs are bounded.

### 3. [MAJOR] `s04_plan`/`s10_copy` read `02_enrich/context.json`, but the PRD artifact name for enrich is split across `context.json` and `narrative.md`.
**Concern.** FR-5.1 has `s02_enrich` write `02_enrich/narrative.md` (the Gemini multimodal output) AND FR-5.4 has it write `02_enrich/context.json` containing a `narrative` field plus `pr_links`, `diff_stats`, `screenshots`. Two artifacts carry the narrative (one as a `.md` file, one as a JSON field). `validate_outputs` runs Pydantic validation on `EnrichedContext`; if `narrative.md` is a declared output it is not schema-covered, and if it is not declared it will not be in `release.zip` nor hash-covered. Downstream consumers (FR-6.1, FR-12.1) reference only `context.json`. This is a minor redundancy that risks a dangling, un-hash-covered file.
**Remediation.** Decide whether `narrative.md` is (a) a human-readable convenience copy that is a declared output of `s02_enrich` (then it is fine, just document it in FR-5.4's output list), or (b) folded entirely into `context.json.narrative` and not written as a separate file. Resolve in Slice 7. Low blast radius; flagged MAJOR only because un-declared outputs escape both `compute_outputs_hash` and the package step.

### 4. [MINOR] `anthropic_model = "claude-opus-4-7"` in `config.toml` is a stale/placeholder model id.
**Concern.** Sub-agents are invoked via `claude -p <name>` subprocess (they pick their own model from the agent frontmatter), so `[models].anthropic_model` in `config.toml` appears unused by the sub-agent path. If it is genuinely unused, it is dead config; if some direct Anthropic call is planned, the model id should be a currently-valid identifier.
**Remediation.** Either remove `anthropic_model` from `config.toml` (no direct Anthropic SDK call is in scope — the plan states "No Anthropic SDK needed") or pin it to a real model id. Cosmetic; handle in Slice 2 alongside Settings.

### 5. [MINOR] Reference plan's "Verification" appendix uses stale stage IDs/paths (`brand_lock`, `09_video`, `10_graphics`, `11_copy`, `12_package`, `04_brand/locked.json`).
**Concern.** The bottom "Verification (end-to-end smoke)" block in the reference plan still references the pre-consolidation 12-stage numbering and a `brand_lock` verb that the plan itself removed. The PRD and use-cases use the correct 11-stage IDs throughout, so this is confined to the plan's appendix and will not mislead implementation if slices are followed — but the smoke-test commands as written would fail.
**Remediation.** Update the plan's verification appendix to the canonical IDs (`shipcast brand`, `08_video/showcase.mp4`, `09_graphics/9x16.png`, `10_copy/twitter_thread.md`, `11_package/release.zip`). Documentation-only; fold into Slice 22/24.

### 6. [MINOR] `06_video_assets` directory naming vs. plan's "06 images" architecture diagram.
**Concern.** The on-disk `_template/` and PRD use `06_video_assets/` with `beat_*.mp4` outputs; the reference plan's architecture diagram still says "06 images → Gemini stills × beats → `06_images/beat_*.png`." The scaffold (`projects/_template/06_video_assets/`) is correct and matches the PRD. The diagram is stale.
**Remediation.** None required for implementation — the scaffold and PRD agree. Optionally fix the plan diagram for consistency. No blast radius.

### 7. [MINOR] `colormath` dependency for ΔE-CIE2000 is unmaintained and imports a deprecated `numpy.asscalar` path on newer numpy.
**Concern.** `pyproject.toml` pins `colormath>=3.0.0` for the palette-conformance ΔE-CIE2000 test (FR-14.5). `colormath` 3.0.0 calls `numpy.asscalar`, removed in numpy ≥ 1.23, which can raise at runtime under the main (non-whisperx) dependency set that does not pin numpy<2.
**Remediation.** Either add a small monkeypatch/shim in the palette-conformance helper, vendor the ΔE-2000 formula directly (it is ~30 lines and removes the dependency), or pin a working numpy in the main set. Handle in Slice 16 when the palette helper lands. Not a boundary issue.

---

## Slices Flagged for Security Pre-Review During Implementation

Confirmed — the set named in `security.md` is correct and complete for shipcast's threat surface:

- **Slice 2 — Cost ledger + dispatcher gate.** Verify the cap check is a true pre-condition (projected total computed before the call) and that `metrics.cost_usd` accumulation cannot underflow or double-count on `--rerun`/reset (reset clears `metrics`, so re-accumulation must start from the surviving stages' totals, not a stale sum).
- **Slice 3 — `input.yaml` validators.** Verify SSRF defenses cover all RFC1918 ranges plus loopback and link-local; confirm hostname is resolved (`socket.gethostbyname`) and the resolved IP is checked, not just the literal; confirm path-traversal defense rejects `..` and symlink escape and that `socket.gethostbyname` is monkeypatchable so unit tests make no network call (AC-2.3).
- **Slice 8 — Playwright client.** Verify the `live_url` validator is invoked BEFORE `goto()` on every path; verify the 60 s navigation timeout is enforced and raises `PlaywrightTimeout`.
- **Slice 10 — `s03_brand` end-to-end.** Verify operator-edit detection via `compute_outputs_hash`; verify NO brand bytes reach `config_snapshot` (byte-equality assertion AC-3.5); if Finding 1 is resolved via option (a), verify the copied `voice.md` is a declared output and hash-covered.
- **Slice 13 — Veo client + standard-mode fallback.** Verify `VeoSafetyBlocked` triggers per-beat fallback WITHOUT writing the original prompt to any log (AC-8.3); verify the 120 s polling timeout and quota-error path.

No additional slices need security pre-review. The sub-agent subprocess slices (7, 11, 12, 19, 20) carry standard timeout/parse-failure handling but no new auth or network-exposure surface beyond the already-flagged `live_url` path.

---

## Module-Boundary Risks (upward-import leak watch)

The layout respects the layering rules. The places most likely to accidentally introduce an upward import during implementation:

1. **`schemas.py` must stay a leaf.** It will accumulate many models (`InputYaml`, `ChangelogEntry`, `EnrichedContext`, `BrandProposal`, `MarketingBrief`, `StoryboardBeat`, `CarouselBeat`, `Storyboard`, `CopyBundle`, `PackageManifest`). Resist importing `cost.py`, `config.py`, or any `clients/*` for "convenience" (e.g., putting a cost constant or a `Settings`-derived default on a model). Cost constants belong in `cost.py`/`config.toml`; keep them out of schema validators. Risk: a validator referencing `Settings.max_cost_usd_per_project` would create `schemas → config` coupling.
2. **`marketing/` and `composition/` must remain pure (no external API).** `hooks.py`, `carousel.py`, `code_screenshot.py`, `captions.py`, `layout.py` are declared "no external API calls." Do not let `s09_graphics` push a `gemini_client` call down into `carousel.py`; the Gemini call belongs in the stage, the composer receives already-generated backgrounds. Risk: `marketing → clients` leak.
3. **`clients/*` must not import `manifest` or `stages`.** `veo_client.py`, `playwright_client.py`, and the extended `gemini_client.py` should depend only on `config`/`errors`. The cost ledger lives in `cost.py` and is invoked by the dispatcher, not by clients. Risk: a client recording its own cost into the manifest would violate "clients do not import manifest" and the cost-gate atomicity (cost must be recorded by the dispatcher AFTER the call returns).
4. **Stages must not import `cli`.** Sub-agent invocation uses the `anthropic_client.py` `claude -p` subprocess pattern, not a back-call into the dispatcher. The dispatcher passes a `clients_factory` into the stage; stages never reach back up for the lock, manifest, or checklist. Risk: a stage importing `cli` to read a flag (e.g., `--no-veo`) — instead, plumb such flags through the stage's `run()` parameters or the manifest/config, as the plan does for `video_mode`.

None of these are present in the scaffold today (it is `__init__.py`-only); they are watch-items for the implementing agents.

---

## Conditions for the PASS

The verdict is PASS provided the following are resolved in their owning slices (none block starting Wave 1):

1. Resolve the `voice.md` read-path gap (Finding 1) in Slice 10.
2. Reconcile the `inputs_hash` drift-detection claim (Finding 2) in Slice 1/4 — either implement the re-check or soften the PRD wording.
3. Decide `narrative.md` vs `context.json` ownership (Finding 3) in Slice 7.

MINOR findings (4–7) are cosmetic or low-blast-radius and may be folded into their natural slices without gating.
