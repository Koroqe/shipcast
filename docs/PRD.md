# shipcast — Product Requirements

shipcast is a Python auto-marketing factory that turns one `CHANGELOG.md` entry from any target software project into a complete, publish-ready marketing package. The operator runs a single CLI command, approves each of eleven human gates, and ends up with a showcase video, static graphics, a LinkedIn carousel, an OG card, an X thread, a LinkedIn long-form post, a blog post, and a ready-to-paste release bundle — all on-brand and factually grounded in the changelog entry.

There is no web interface, no database, and no remote server. The system is designed for a solo operator running on macOS or Linux. Every stage produces artifacts that the operator reviews and explicitly approves before the next stage can start.

**First target:** `example-project` (live web app with a reachable URL).

**Scaffold reuse.** The CLI dispatcher, manifest abstraction, Stage protocol, human-gate enforcement, locking, and client patterns are carried over verbatim from a proven upstream pipeline scaffold; only the stage classes, schemas, marketing modules, and sub-agents are new.

## Sections

| # | Feature / Stage | Status |
|---|-----------------|--------|
| 1 | [Core Scaffold and Core Invariants](#1-core-scaffold-and-core-invariants) | Defined |
| 2 | [Input Contract and Validation](#2-input-contract-and-validation) | Defined |
| 3 | [Brand Pack Contract and s03_brand](#3-brand-pack-contract-and-s03_brand) | Defined |
| 4 | [Stage 01: Pick Changelog Entry (s01_pick)](#4-stage-01-pick-changelog-entry-s01_pick) | Defined |
| 5 | [Stage 02: Enrich Context (s02_enrich)](#5-stage-02-enrich-context-s02_enrich) | Defined |
| 6 | [Stage 04: Marketing Plan (s04_plan)](#6-stage-04-marketing-plan-s04_plan) | Defined |
| 7 | [Stage 05: Script and Storyboard (s05_script)](#7-stage-05-script-and-storyboard-s05_script) | Defined |
| 8 | [Stage 06: Video Assets (s06_video_assets)](#8-stage-06-video-assets-s06_video_assets) | Defined |
| 9 | [Stage 07: Voice Synthesis (s07_voice)](#9-stage-07-voice-synthesis-s07_voice) | Defined |
| 10 | [Stage 08: Video Assembly (s08_video)](#10-stage-08-video-assembly-s08_video) | Defined |
| 11 | [Stage 09: Static Graphics (s09_graphics)](#11-stage-09-static-graphics-s09_graphics) | Defined |
| 12 | [Stage 10: Copy Generation (s10_copy)](#12-stage-10-copy-generation-s10_copy) | Defined |
| 13 | [Stage 11: Package (s11_package)](#13-stage-11-package-s11_package) | Defined |
| 14 | [Marketing Strategy Constraints](#14-marketing-strategy-constraints) | Defined |
| 15 | [Two Video Modes](#15-two-video-modes) | Defined |
| 16 | [Non-Functional Requirements](#16-non-functional-requirements) | Defined |
| 17 | [Out of Scope for v1](#17-out-of-scope-for-v1) | Defined |

---

## 1. Core Scaffold and Core Invariants

### Feature Description

The core scaffold is the foundational harness that all pipeline stages are built on. It delivers: the repo structure, a `Manifest` abstraction that tracks every stage's status in a single `manifest.json` per project, a Typer CLI dispatcher that enforces human-approval gates between stages, and the concurrency, hashing, logging, and error infrastructure. This section maps to Slices 1–4 of the implementation plan.

The scaffold is carried over verbatim from a proven upstream pipeline (with package rename) and then extended with shipcast-specific stage IDs, settings, and the cost ledger.

### User Story

As a marketing operator, I want a CLI that lets me create a project from a changelog entry, run each pipeline stage, inspect the output, and explicitly approve it before the next stage starts, so that I maintain full control over every artifact before committing to downstream AI spend.

### Functional Requirements

**Project lifecycle**

- FR-1.1: `shipcast pick <repo-path> --entry "<heading>"` creates a new project at `projects/<repo-short>--<entry-slug>/` by reading `input.yaml`, writing `manifest.json` with all eleven stages set to `pending`, and recording a `config_snapshot` from `config.toml` at creation time. Raises `ChangelogEntryNotFound` and exits non-zero if the entry heading does not match any entry in the target CHANGELOG.md. Never auto-creates a CHANGELOG.md.
- FR-1.2: `shipcast status <slug>` renders an eleven-row color-coded table (pending=grey, running=yellow, done=green, failed=red, needs_review=cyan) showing stage id, status, and whether `human_approved_at` is set.
- FR-1.3: The CLI package entry point is `shipcast` (registered in `pyproject.toml`). Running `shipcast --help` exits 0 and lists all eleven verb names.

**Per-project directory layout**

- FR-1.4: Every project lives under `projects/<slug>/` and contains: `manifest.json`, `input.yaml`, `.lock`, `logs/`, and subdirectories `01_pick/` through `11_package/`. `manifest.json` is the single source of truth; no sidecar status files are written.
- FR-1.5: `projects/_template/` is the canonical seed with `.gitkeep` files so every stage subfolder is tracked. `shipcast pick` uses `shutil.copytree` to instantiate it.
- FR-1.6: `projects/_brand/<brand_slug>/` holds operator-supplied brand assets required before `s03_brand` can run (see Section 3).

**Manifest**

- FR-1.7: `manifest.json` carries `schema_version: 1`. Each of the eleven stages has a `StageRecord` containing: `status`, `outputs` (list of relative paths), `inputs_hash`, `outputs_hash_at_done`, `started_at`, `finished_at`, `human_approved_at`, `manually_edited`, `error`, and `metrics` (including `cost_usd`).
- FR-1.8: `Manifest.transition(stage_id, new_status)` enforces these legal transitions only; all others raise `IllegalTransition`:
  - `pending → running`
  - `running → done`
  - `running → failed`
  - `running → needs_review`
  - `needs_review → running`
  - `failed → running`
  - `failed → pending`
  - `done → pending`
- FR-1.9: Manifest writes are atomic: serialized to `manifest.json.tmp`, `fsync`-ed, then moved via `os.replace`. A mid-write crash leaves `manifest.json` unmodified.
- FR-1.10: All manifest and artifact JSON uses `sort_keys=True, indent=2, ensure_ascii=False, separators=(",", ": ")` plus a trailing newline, making output byte-deterministic.
- FR-1.11: `config_snapshot` is written once on project creation. Once any stage transitions out of `pending`, further attempts to update `config_snapshot` raise `ConfigSnapshotLocked`. Brand data is never written into `config_snapshot` (it lives as files under `03_brand/`).
- FR-1.12: Loading a `manifest.json` with `schema_version` other than `1` raises `ManifestMigrationNeeded`.
- FR-1.13: Two distinct hash families, intentionally asymmetric:
  - `compute_inputs_hash(paths)` — fast SHA-256 over sorted `(relative_path, mtime_ns, size_bytes)` tuples. Used for `StageRecord.inputs_hash` (upstream invalidation). False positives are cheap (re-run); false negatives are avoided by the mtime+size combination.
  - `compute_outputs_hash(paths)` — byte-content SHA-256 over sorted `(relative_path, sha256_of_file_bytes)` tuples. Used for `StageRecord.outputs_hash_at_done` and `shipcast approve` recomputation. Detects any manual edit, including no-op mtime touches and same-size byte swaps.

**Human-approval gate**

- FR-1.14: After a stage finishes with `status=done`, all downstream stages refuse to start until `shipcast approve <slug> <stage_id>` is run. Attempting to run a stage whose upstream is done but not approved raises `StageNotApproved` (exit code 2).
- FR-1.15: `shipcast approve <slug> <stage_id>` recomputes `compute_outputs_hash` over all output files. If the recomputed value differs from `outputs_hash_at_done`, the manifest records `manually_edited: true` and the CLI lists each changed file. This is how the operator's edits to `03_brand/proposal.json` are detected.
- FR-1.16: `shipcast approve` raises `CannotApproveNonDoneStage` (exit code 1) when the stage is not in `done` status.

**Stage re-run and reset**

- FR-1.17: Every per-stage CLI command accepts a `--rerun` flag. When the stage is `done`, `--rerun` transitions `done → pending` (resetting downstream stages transitively) before re-executing. When the stage is `pending` or `failed`, `--rerun` is a no-op. When `running`, `--rerun` raises `StageBusy`.
- FR-1.18: `shipcast reset <slug> <stage_id> [--yes]` prompts for confirmation unless `--yes` is passed, deletes the stage's output files, calls `Manifest.reset` to clear all fields for that stage, and transitively resets all downstream stages.
- FR-1.19: `shipcast <verb> --rerun` and `shipcast reset` against a stage that has downstream `human_approved_at` non-null refuse without `--yes`, and list every approval that will be discarded before proceeding. This is the cascade-confirmation guard.

**CLI dispatcher**

- FR-1.20: The dispatcher (not the stage) owns: load project → acquire lock → (if `--rerun` and `done`) reset → transition to `running` → `check_inputs` → `run` → `validate_outputs` → compute `outputs_hash_at_done` → transition to `done` → save manifest → release lock → print Review Checklist.
- FR-1.21: Any exception from `check_inputs` or `run` causes the dispatcher to transition the stage to `failed`, record structured error info (traceback path under `logs/<utcnow-iso>.log`), save the manifest, release the lock, and exit non-zero.
- FR-1.22: After every successful stage run, the dispatcher prints a `rich.Panel` Review Checklist with: absolute paths to every output artifact, the stage's `review_checklist_items` bullets, and verbatim instructions for `--rerun`, hand-edit-then-approve, and `reset`.

**Stage protocol**

- FR-1.23: Every stage class implements the `Stage` protocol: `id: str`, `requires: tuple[str, ...]`, `check_inputs(project)`, `run(project) -> StageResult`, `validate_outputs(project, result)`, `review_checklist_items: tuple[str, ...]` (at least 3 items), and `output_schema: type[BaseModel]`.
- FR-1.24: `BaseStage.check_inputs` verifies every stage in `requires` has `status=done` AND `human_approved_at` non-null AND all declared upstream output files exist on disk AND the stored `inputs_hash` still matches. Raises `StageInputMissing` or `StageNotApproved` as appropriate.
- FR-1.25: `BaseStage.additional_input_paths(project)` is an optional hook (default empty) for stages that consume operator-placed files outside the upstream stage's `outputs` (e.g., `s03_brand`'s brand pack directory).
- FR-1.26: `BaseStage.pre_run_hook` is a class-level no-op test seam. Production code must not read environment variables to alter stage behavior.

**Cost ledger**

- FR-1.27: `src/shipcast/cost.py` defines `CostLedger` which accumulates per-stage USD cost into `manifest.stages[id].metrics.cost_usd`. Per-tool unit-cost constants: Veo 3 Fast = $3.20/clip (premium mode only), Gemini Imagen = $0.04/image, Gemini multimodal = $0.01/call, ElevenLabs = $0.30/min.
- FR-1.28: `Settings.max_cost_usd_per_project` is mode-dependent: $3.00 for standard, $8.00 for premium (sourced from `input.yaml.video_mode`). The dispatcher checks accumulated cost BEFORE invoking any stage that calls Veo, Gemini Imagen, Gemini multimodal, or ElevenLabs. If the next call would push the total past the cap, the dispatcher raises `CostCapExceeded` and exits non-zero.
- FR-1.29: `config_snapshot` serializes only the "public" subset of `Settings` (model IDs, voice IDs, durations, cost constants). `SecretStr` fields (API keys) are never written to `config_snapshot` or any log output.

**Concurrency and platform**

- FR-1.30: Every CLI dispatch acquires an advisory `fcntl.flock(LOCK_EX | LOCK_NB)` on `projects/<slug>/.lock`. If already held, the dispatcher raises `ProjectLocked` (exit code 2).
- FR-1.31: On platforms other than macOS and Linux, the CLI fails at startup with `UnsupportedPlatform` before any operation.
- FR-1.32: `--no-lock` is only honored when `SHIPCAST_NO_LOCK_ACK=1` is also set in the environment. Without the ack, the dispatcher raises `LockBypassNotAcknowledged` and exits non-zero. Even with the ack, a yellow warning banner is printed.

**Logging**

- FR-1.33: `logging_setup.configure(project)` initializes a `RichHandler` console logger at INFO level plus a JSON-line file handler writing to `projects/<slug>/logs/<utcnow-iso>.log`. Tracebacks written to the log file must not contain `SecretStr` raw values (Pydantic's `__repr__` masks them).

### Non-Functional Requirements

- NFR-1.1: Python 3.12+, managed with `uv`. `mypy --strict src/shipcast` must report zero errors. `ruff check src tests` must report zero findings.
- NFR-1.2: `manifest.py` must achieve 100% line coverage. Package-wide coverage must be ≥ 90%.
- NFR-1.3: macOS and Linux only. `fcntl.flock` is the concurrency primitive.
- NFR-1.4: Manifest writes use the temp-file + `os.replace` pattern. A mid-write crash leaves `manifest.json` unmodified.
- NFR-1.5: No real external API calls in unit tests. Stage tests inject mock clients via `clients_factory`.

### Acceptance Criteria

- AC-1.1: `uv run shipcast --help` exits 0 and lists all eleven verb names.
- AC-1.2: `uv run mypy --strict src/shipcast` reports zero errors.
- AC-1.3: `uv run ruff check src tests` reports zero findings.
- AC-1.4: `uv run pytest -v` passes; `shipcast.manifest` coverage is 100%; package overall coverage is ≥ 90%.
- AC-1.5: `shipcast pick ../example-project --entry "<heading>"` creates `projects/<slug>/manifest.json` with all eleven stages in `pending` and a populated `config_snapshot` that contains no API key values.
- AC-1.6: Running any stage whose upstream is `done` but not approved exits with code 2 and prints `StageNotApproved`.
- AC-1.7: `shipcast approve <slug> <stage_id>` when stage is not `done` exits with code 1 and prints `CannotApproveNonDoneStage`.
- AC-1.8: Manually editing any output file and then running `shipcast approve` prints "Manual edits detected on N files" and records `manually_edited: true`.
- AC-1.9: Starting two concurrent `shipcast` invocations against the same slug results in exactly one exit-0 and one exit-2 (`ProjectLocked`).
- AC-1.10: Running `shipcast` on a platform other than macOS/Linux exits with `UnsupportedPlatform`.
- AC-1.11: A mock-cost test asserts the dispatcher aborts with `CostCapExceeded` when projected accumulated cost exceeds the mode cap, and proceeds otherwise.
- AC-1.12: `.env.example` contains only bare variable names with empty values; a test asserts no real key values are present.
- AC-1.13: Simulating a mid-write crash (monkeypatching `os.replace`) leaves the original `manifest.json` unmodified.

### Affected Components

| Path | Change |
|------|--------|
| `pyproject.toml` | New — `shipcast` entry point, dependencies |
| `config.toml` | New — model IDs, voice ID, cost constants, durations |
| `.env.example` | New — key names only: `ANTHROPIC_API_KEY=`, `ELEVENLABS_API_KEY=`, `GEMINI_API_KEY=` |
| `src/shipcast/manifest.py` | New — carried over from the upstream scaffold with package rename |
| `src/shipcast/cost.py` | New — `CostLedger`, per-tool unit-cost constants |
| `src/shipcast/cli.py` | New — Typer app, 11 verbs, cost-cap gate |
| `src/shipcast/project.py` | New — `Project.create`, `Project.load`, path helpers |
| `src/shipcast/stage.py` | New — `Stage` protocol, `StageResult` |
| `src/shipcast/stages/_base.py` | New — `BaseStage` |
| `src/shipcast/stages/s01_pick.py … s11_package.py` | New — 11 concrete stage files |
| `src/shipcast/errors.py` | New — all custom exception classes |
| `src/shipcast/locking.py` | New — `fcntl.flock` context manager |
| `src/shipcast/logging_setup.py` | New — `configure()` |
| `src/shipcast/config.py` | New — `Settings` via pydantic-settings |
| `projects/_template/` | New — seed directory |

---

## 2. Input Contract and Validation

### Feature Description

Every shipcast project is driven by an `input.yaml` file that the operator places in the project directory before running `shipcast pick`. This section defines the schema for that file, the validators that reject unsafe inputs before any external API call is made, and the test coverage that proves every rejection path works. Maps to Slice 3.

### User Story

As a security-conscious operator, I want the CLI to reject malformed, dangerous, or out-of-scope input values immediately at parse time, so that I cannot accidentally point Playwright at an internal network address or introduce a path-traversal vulnerability.

### Functional Requirements

- FR-2.1: `InputYaml` is a Pydantic v2 model defined in `src/shipcast/schemas.py` with these fields:
  - `repo_path: Path` — path to the target software project
  - `entry_heading: str` — exact heading from that project's CHANGELOG.md
  - `live_url: AnyUrl | None` — optional URL to the live web app for Playwright brand extraction and enrichment screenshots
  - `brand_slug: str` — key into `projects/_brand/<brand_slug>/`
  - `video_mode: Literal["standard", "premium"] = "standard"`
  - `feature_walkthrough: list[WalkthroughStep] | None` — optional Playwright script for enrichment screenshots
- FR-2.2: `WalkthroughStep` is a Pydantic v2 model with fields `action: Literal["goto", "click", "type", "wait", "screenshot"]`, `selector: str | None`, and `value: str | None`.
- FR-2.3: `live_url` validators (applied in order; first match raises `ValueError`):
  - Reject any scheme other than `https://` (blocks `http://`, `ftp://`, `file://`, `javascript:`, etc.)
  - Resolve the hostname via `socket.gethostbyname`; reject if `ipaddress.IPv4Address(...).is_private` is True (covers all RFC 1918 ranges: 10.x, 172.16–31.x, 192.168.x)
  - Reject if `ipaddress.IPv4Address(...).is_loopback` is True (covers 127.x and `::1`)
  - Reject if `ipaddress.IPv4Address(...).is_link_local` is True (covers 169.254.x)
- FR-2.4: `repo_path` validators:
  - Reject any path that contains a `..` segment (path traversal)
  - Reject any path not under `/Users/aleksei/Documents/Projects.nosync/`
  - Reject if the resolved path does not contain a `CHANGELOG.md` file
- FR-2.5: `WalkthroughStep.selector` validators:
  - Reject any value containing the substring `javascript:` (XSS via Playwright `evaluate`)
  - Reject any value that is a CSS pseudo-element or pseudo-class not expressible as a plain selector
- FR-2.6: `WalkthroughStep.action` must be one of the five allowed literals; any other value raises `ValueError`.
- FR-2.7: `InputYaml` parsing is triggered as the first step of `s01_pick.check_inputs()`. Any validation error transitions the stage to `failed` and records a structured error message listing every failing field.

### Non-Functional Requirements

- NFR-2.1: All validation logic is pure Python (no external network calls) and completes in under 10 ms.
- NFR-2.2: The URL validation must cover the full RFC 1918 private address space. A test asserts each of the three RFC 1918 ranges is rejected independently.

### Acceptance Criteria

- AC-2.1: `tests/unit/test_input_validation.py` covers exactly these rejection cases and exits passing:
  - `http://` scheme URL
  - `ftp://` scheme URL
  - RFC 1918 192.168.x URL
  - RFC 1918 10.x URL
  - RFC 1918 172.16.x URL
  - `localhost` URL (loopback)
  - Link-local 169.254.x URL
  - `repo_path` containing `..`
  - `repo_path` outside the allowed root
  - `repo_path` that exists but has no `CHANGELOG.md`
  - `WalkthroughStep` with unknown action
  - `WalkthroughStep.selector` containing `javascript:`
  - `WalkthroughStep` with missing required field
- AC-2.2: Two "accept" cases pass: valid `standard` mode input and valid `premium` mode input.
- AC-2.3: Validation never makes a network call (confirmed by monkeypatching `socket.gethostbyname` in tests).

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/schemas.py` | New — `InputYaml`, `WalkthroughStep` models |
| `tests/unit/test_input_validation.py` | New — 13 rejection + 2 accept tests |

---

## 3. Brand Pack Contract and s03_brand

### Feature Description

`s03_brand` is the third pipeline stage. It reads a pre-populated brand pack from `projects/_brand/<brand_slug>/`, optionally extracts additional brand data from the live web app via Playwright, produces a `proposal.json` for operator review and edit, and writes `logo.png` and `style_sheet.png`. Downstream stages read brand data from the `03_brand/` file tree; brand data never enters `config_snapshot`.

The operator edits `proposal.json` (and optionally replaces `logo.png` or `style_sheet.png`) between stage completion and `shipcast approve`. The standard `compute_outputs_hash` mechanism automatically detects edits and records `manually_edited: true`.

Maps to Slices 5 (changelog parser), 8 (Playwright client), 9 (Gemini aspect-ratio extension), and 10 (s03_brand stage integration).

### User Story

As an operator, I want the pipeline to automatically propose a color palette, font, and logo from the live app URL, and then let me review and correct the proposal before locking it, so that every downstream artifact reflects accurate brand identity.

### Functional Requirements

**Brand pack directory (required before s03_brand runs)**

- FR-3.1: The brand pack directory `projects/_brand/<brand_slug>/` must contain:
  - `voice.md` — REQUIRED. Documents tone, banned phrases, signature phrases, CTA pattern, motion style, and `caption_mode: chip|karaoke|reveal`.
  - `fonts/` — REQUIRED. Must contain at least one `.ttf` file (display font); a second `.ttf` for body is optional.
  - `logo.svg` — REQUIRED. SVG preferred; a PNG with transparency is acceptable as `logo.png`.
- FR-3.2: The following files in the brand pack are optional:
  - `palette.hint.json` — pre-seeded `{primary, accent, neutral}` hex values; when present, `s03_brand` uses it directly and skips the Playwright palette-extraction pass entirely.
  - `music/` — one to three `.mp3` or `.wav` files for background music; operator-supplied.
  - `style_sheet.png` — operator-supplied reference image; if present, `s03_brand` uses it directly and skips the Gemini style-sheet generation call.
- FR-3.3: If any REQUIRED file is absent, `s03_brand.check_inputs()` raises `BrandPackIncomplete` listing every missing file. No external API call is made before this check passes.

**Playwright extraction (skipped when `palette.hint.json` is present)**

- FR-3.4: `playwright_client.py` exposes three methods: `extract_css_palette(url) -> list[str]` (top-5 hex colors by pixel frequency from the rendered viewport screenshot), `extract_font_family(url) -> str` (computed `font-family` of `body` and `h1`), `screenshot_logo(url) -> bytes | None` (screenshots the bounding box of the first element matching `[class*=logo i]`, `[id*=logo i]`, `header img:first-of-type`, or `header svg:first-of-type`; returns `None` if no match).
- FR-3.5: Navigation timeout is 60 seconds. If exceeded, `playwright_client` raises `PlaywrightTimeout`; the stage fails and the operator reruns after confirming the URL.
- FR-3.6: The `live_url` validator from Section 2 is called before any `playwright_client` method. Playwright is never pointed at a URL that failed validation.

**Gemini style-sheet generation (skipped when `style_sheet.png` is present in brand pack)**

- FR-3.7: `s03_brand` calls `gemini_client.generate_image(prompt, aspect_ratio="1:1")` to produce `03_brand/style_sheet.png`. This is a direct REST call; it is not routed through a `claude -p` sub-agent.

**Proposal outputs**

- FR-3.8: `s03_brand` writes exactly three artifacts to `03_brand/`:
  - `proposal.json` — Pydantic-validated `BrandProposal` with fields: `palette: list[str]` (five hex candidates from extraction, or the three from `palette.hint.json`), `font_family: str`, `logo_detected: bool`.
  - `logo.png` — PNG; 1×1 transparent if `logo_detected == false`.
  - `style_sheet.png` — Gemini-generated or operator-supplied 1:1 PNG.
- FR-3.9: After the stage completes and before `shipcast approve`, the operator may edit `proposal.json` to reduce the palette to exactly three hex codes (`primary`, `accent`, `neutral`) and correct font/logo. The operator may also replace `logo.png` or `style_sheet.png` bytes.
- FR-3.10: `shipcast approve <slug> 03_brand` recomputes `compute_outputs_hash`. If any file changed, `manually_edited: true` is recorded and the changed files are listed.
- FR-3.11: Brand data (palette, fonts, logo bytes) is never written into `config_snapshot`. Downstream stages read from `03_brand/` files; `inputs_hash` for each downstream stage covers those files, so brand drift correctly triggers re-run prompts.

**Aspect-ratio extension for Gemini client**

- FR-3.12: `gemini_client.generate_image()` accepts an `aspect_ratio: Literal["1:1", "16:9", "9:16", "4:5", "og"]` parameter. The `"og"` value maps to 1200×630 pixels for the OG card (see Section 11). Existing callers that do not pass this parameter continue to receive 16:9 images.

### Non-Functional Requirements

- NFR-3.1: `s03_brand` must not mutate `config_snapshot` under any code path.
- NFR-3.2: Missing brand pack files produce a human-readable error listing every missing file path before any external API call.

### Acceptance Criteria

- AC-3.1: `s03_brand` raises `BrandPackIncomplete` (listing missing files) when `voice.md`, `fonts/`, or `logo.svg` is absent; no API call is made.
- AC-3.2: With a complete brand pack (no `palette.hint.json`), `s03_brand` produces `03_brand/proposal.json`, `03_brand/logo.png`, and `03_brand/style_sheet.png` with valid file headers. `proposal.json` validates against `BrandProposal` schema.
- AC-3.3: With `palette.hint.json` present, the Playwright palette-extraction pass is skipped (confirmed by asserting `playwright_client.extract_css_palette` is never called).
- AC-3.4: Editing `proposal.json` bytes and then running `shipcast approve` records `manually_edited: true` and lists the changed file.
- AC-3.5: `manifest.json` `config_snapshot` does not change before and after `s03_brand` runs (byte-equality assertion).
- AC-3.6: `playwright_client` methods raise `PlaywrightTimeout` when navigation exceeds 60 s (mocked in tests). The URL validator is invoked before any Playwright call.
- AC-3.7: Each `gemini_client.generate_image(aspect_ratio=X)` call in tests asserts the resulting PIL image size matches the named aspect ratio. Existing `aspect_ratio=None` calls produce 16:9 images (regression test).

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/clients/playwright_client.py` | New |
| `src/shipcast/clients/gemini_client.py` | Extended — `aspect_ratio` parameter |
| `src/shipcast/brand/extractor.py` | New — composes Playwright + PIL palette extraction |
| `src/shipcast/stages/s03_brand.py` | New |
| `src/shipcast/schemas.py` | Extended — `BrandProposal` model |
| `projects/_brand/` | New — operator-populated brand packs |

---

## 4. Stage 01: Pick Changelog Entry (s01_pick)

### Feature Description

`s01_pick` is the entry-point stage. It reads `input.yaml`, parses the target project's `CHANGELOG.md`, locates the requested entry by heading match, and writes a validated `entry.json` artifact. No AI or external network calls are made; this stage is fully deterministic.

Maps to Slices 5 (changelog parser) and 6 (s01_pick stage).

### User Story

As an operator, I want to register a specific changelog entry as a shipcast project so that all downstream stages have a validated, structured source of truth for the feature being marketed.

### Functional Requirements

- FR-4.1: `changelog/parser.py` parses a `CHANGELOG.md` file into `list[ChangelogEntry]` using a hand-rolled scanner that recognises `## YYYY-MM-DD` day headings and `### <name> — HH:MM UTC` entry headings (the format defined by the global changelog rule).
- FR-4.2: The parser handles: canonical format, multiple entries per day, missing time field (records `None`), and empty file (returns empty list). It raises `ChangelogFileMissing` if the file does not exist; it never auto-creates it.
- FR-4.3: `s01_pick.run()` calls the parser, locates the entry whose heading exactly matches `input_yaml.entry_heading` (case-insensitive, trimmed), and raises `ChangelogEntryNotFound` if no match is found.
- FR-4.4: `s01_pick` writes `01_pick/entry.json` serialized with the standard determinism rules (FR-1.10). The file validates against `ChangelogEntry` schema.
- FR-4.5: Running `s01_pick` twice on the same `input.yaml` and unchanged `CHANGELOG.md` produces byte-identical `entry.json` both times.

### Acceptance Criteria

- AC-4.1: Parser unit tests pass for five fixture changelogs: canonical format, missing day heading, missing time, multiple entries per day, empty file.
- AC-4.2: Integration test runs `shipcast pick <fixture-repo> --entry "<heading>"` end-to-end and asserts `manifest.stages.01_pick.status == "done"` and `01_pick/entry.json` byte-equality against a pinned fixture.
- AC-4.3: Running `s01_pick` with a non-existent `CHANGELOG.md` raises `ChangelogFileMissing` (stage transitions to `failed`).
- AC-4.4: Running `s01_pick` with a heading that does not appear in the changelog raises `ChangelogEntryNotFound`.
- AC-4.5: Two runs on the same input produce byte-identical `entry.json` (idempotency assertion).

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/changelog/parser.py` | New |
| `src/shipcast/stages/s01_pick.py` | New |
| `src/shipcast/schemas.py` | Extended — `ChangelogEntry` model |

---

## 5. Stage 02: Enrich Context (s02_enrich)

### Feature Description

`s02_enrich` extracts the feature context needed to produce grounded marketing copy. It combines three sub-steps: (a) `gh pr list` and `git log` to surface PR links and diff stats; (b) Playwright walkthrough of `live_url` to capture real-app screenshots; and (c) a Gemini multimodal call that synthesizes screenshots, diff stats, and entry text into a narrative. If `live_url` is absent, sub-step (b) is skipped.

Maps to Slice 7.

### User Story

As an operator, I want the pipeline to automatically gather PR links, diff statistics, and real-app screenshots for the changelog entry so that the marketing copy and video are factually grounded rather than hallucinated.

### Functional Requirements

- FR-5.1: `s02_enrich` runs three sub-steps in sequence: (a) `gh pr list --json` + `git log --stat` in the target `repo_path` to produce `pr_links: list[str]` and `diff_stats: dict`; (b) Playwright walkthrough using `input_yaml.feature_walkthrough` steps to capture screenshots into `02_enrich/screenshots/`; (c) `gemini_client.multimodal(prompt, images)` call passing the screenshots (if any), diff stats, and entry text to produce `02_enrich/narrative.md`.
- FR-5.2: If `live_url` is absent from `input.yaml`, sub-step (b) is skipped and logged. Sub-step (c) proceeds using only diff stats and entry text.
- FR-5.3: `s02_enrich` invokes the `ba-analyst` sub-agent via `subprocess.run(["claude", "-p", "ba-analyst", ...], timeout=300)` for high-level framing. On timeout, the stage fails with `SubagentTimeout`. On non-zero exit, the stage fails with stderr captured into `error`. On JSON parse failure of stdout, the stage fails with `SubagentMalformedOutput`.
- FR-5.4: `s02_enrich` writes `02_enrich/context.json` containing `pr_links`, `diff_stats`, `narrative`, and `screenshots` (list of relative paths). The artifact validates against `EnrichedContext` schema.
- FR-5.5: `gemini_client.multimodal(prompt, images)` is an extension of the existing `gemini_client.py` against the Gemini 2.5 Pro endpoint with image parts. It is not a separate client or sub-agent invocation.

### Acceptance Criteria

- AC-5.1: Integration test asserts `02_enrich/context.json` contains `pr_links: list[str]`, `diff_stats: dict`, `narrative: str` (non-empty), `screenshots: list[str]` (≥ 1 PNG path when `live_url` is provided).
- AC-5.2: When `live_url` is absent, `screenshots` is an empty list and `narrative` is still non-empty.
- AC-5.3: Sub-agent timeout test asserts stage fails with `SubagentTimeout` and `status=failed` when the subprocess call exceeds 300 s.
- AC-5.4: Gemini multimodal rate-limit mock asserts stage fails with `GeminiRateLimited`.

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/stages/s02_enrich.py` | New |
| `src/shipcast/clients/gemini_client.py` | Extended — `multimodal()` method |
| `src/shipcast/schemas.py` | Extended — `EnrichedContext` model |

---

## 6. Stage 04: Marketing Plan (s04_plan)

### Feature Description

`s04_plan` produces the `MarketingBrief` that all creative stages consume. It runs two chained sub-agent calls: first `planner` to draft the brief, then `brand-guardian` to validate the draft against the brand pack. The brief locks in the hook template per channel, the video beat structure, the carousel beat structure, and conditional flags for the stat card and code screenshot.

Maps to Slice 11.

### User Story

As an operator, I want a structured marketing brief derived from the enriched context and brand guidelines so that all downstream creative stages produce consistent, on-brand, channel-appropriate output.

### Functional Requirements

- FR-6.1: `s04_plan` invokes the `planner` sub-agent via `claude -p` (300 s timeout) with the `01_pick/entry.json` and `02_enrich/context.json` as context. The sub-agent returns a draft `MarketingBrief` JSON.
- FR-6.2: `s04_plan` then invokes the `brand-guardian` sub-agent via `claude -p` (300 s timeout), passing the draft brief and `03_brand/proposal.json` + `03_brand/voice.md`. Brand-guardian may modify the brief; its output is the final brief.
- FR-6.3: The chained invocations are sequential subprocesses. Failure in either raises the standard sub-agent error classes (`SubagentTimeout`, `SubagentMalformedOutput`).
- FR-6.4: `MarketingBrief` schema (defined in `schemas.py`) requires:
  - `hook_template_per_channel: dict[Literal["x", "linkedin", "blog"], str]` — each value must be one of the seven catalog keys defined in `src/shipcast/marketing/hooks.py`.
  - `ctas: list[str]` — at least one call-to-action string.
  - `video_beats: list[StoryboardBeat]` — exactly 4 items (one hero + three fill shots).
  - `carousel_beats: list[CarouselBeat]` — exactly 4 items (maps to carousel slides 2–5; slide 1 is hook, slide 6 is CTA).
  - `has_stat_card: bool` — whether the entry warrants a stat-card graphic.
  - `has_code_screenshot: bool` — whether the entry warrants a code-screenshot graphic.
- FR-6.5: `src/shipcast/marketing/hooks.py` defines the fixed catalog of seven hook templates: `we_just_shipped`, `before_after`, `problem_aha`, `numbered_list`, `behind_the_scenes`, `5_sec_value`, `social_proof`. Each template exposes a `render(entry: ChangelogEntry) -> str` function.
- FR-6.6: `s04_plan` writes `04_plan/brief.json` validating against `MarketingBrief` schema.

### Acceptance Criteria

- AC-6.1: Integration test produces `04_plan/brief.json` that validates against `MarketingBrief` schema with `video_beats` length == 4, `carousel_beats` length == 4, and each `hook_template_per_channel` value present in the catalog.
- AC-6.2: Catalog unit test: each of the seven templates renders a non-empty string when given a sample `ChangelogEntry`.
- AC-6.3: `brand-guardian` agent file exists at `~/.claude/agents/brand-guardian.md` with valid frontmatter.
- AC-6.4: Sub-agent timeout on either chained call transitions the stage to `failed` with `SubagentTimeout`.

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/stages/s04_plan.py` | New |
| `src/shipcast/marketing/hooks.py` | New — hook catalog + render functions |
| `src/shipcast/schemas.py` | Extended — `MarketingBrief`, `StoryboardBeat`, `CarouselBeat` |
| `~/.claude/agents/brand-guardian.md` | New — user-level sub-agent definition |

---

## 7. Stage 05: Script and Storyboard (s05_script)

### Feature Description

`s05_script` produces the showcase video storyboard: four to six beats, each with an image prompt, a narration line, and a duration. The `demo-script-writer` sub-agent drafts the storyboard guided by the marketing brief.

Maps to Slice 12.

### User Story

As an operator, I want a structured storyboard so that the image generation and voice synthesis stages have clear, brief-consistent inputs and I can review the narrative arc before any expensive media is generated.

### Functional Requirements

- FR-7.1: `s05_script` invokes the `demo-script-writer` sub-agent via `claude -p` (300 s timeout), passing `04_plan/brief.json` and `01_pick/entry.json` as context.
- FR-7.2: The sub-agent returns a JSON-serializable `Storyboard` with `beats: list[StoryboardBeat]`, where each beat has `image_prompt: str`, `narration: str`, and `duration_sec: float`.
- FR-7.3: The storyboard must contain between 4 and 6 beats inclusive. Fewer or more raises `SubagentMalformedOutput`.
- FR-7.4: `s05_script` writes `05_script/storyboard.json` validating against `Storyboard` schema.

### Acceptance Criteria

- AC-7.1: Integration test produces `05_script/storyboard.json` with 4–6 beats, each beat having `image_prompt`, `narration`, and `duration_sec` fields.
- AC-7.2: `demo-script-writer` agent file exists at `~/.claude/agents/demo-script-writer.md` with valid frontmatter.
- AC-7.3: A snapshot test in `tests/unit/test_subagents.py` mocks the `claude -p` subprocess call and asserts the parsing of expected output structure (JSON shape, field presence, length constraints).

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/stages/s05_script.py` | New |
| `src/shipcast/schemas.py` | Extended — `Storyboard` model |
| `~/.claude/agents/demo-script-writer.md` | New — user-level sub-agent definition |

---

## 8. Stage 06: Video Assets (s06_video_assets)

### Feature Description

`s06_video_assets` renders one MP4 clip per video beat. In `standard` mode every beat is a Gemini Imagen still animated with a Ken-Burns pan/zoom. In `premium` mode beat[0] is a Veo 3 Fast native-motion clip (8 s); beats[1..3] are Imagen + Ken-Burns. On a Veo safety block, the affected beat falls back to Imagen + Ken-Burns. On a Veo quota error, the stage fails.

Maps to Slice 13.

### User Story

As an operator, I want per-beat video clips generated automatically from the storyboard so that the video assembly stage has ready-to-concatenate footage, with quality calibrated to whether I've chosen standard or premium mode.

### Functional Requirements

- FR-8.1: `s06_video_assets` iterates `brief.video_beats` (exactly 4 beats from `04_plan/brief.json`).
- FR-8.2: In `standard` mode, every beat is rendered by calling `gemini_client.generate_image(beat.image_prompt, aspect_ratio="9:16")` to produce a still, then `ffmpeg_client.ken_burns_clip(still, duration=beat.duration_sec)` to produce a 1080×1920 h264 MP4.
- FR-8.3: In `premium` mode, beat[0] is rendered by `veo_client.generate_clip(beat.image_prompt, conditioning_image=style_sheet_path)` producing an 8 s MP4. Beats[1..3] are rendered as in standard mode.
- FR-8.4: `veo_client.py` wraps the Gemini Veo 3 Fast REST endpoint (`/v1beta/models/veo-3-fast:generateContent`). It polls until the job completes or raises `VeoTimeout` after 120 s. It raises `VeoQuotaExceeded` on quota errors and `VeoSafetyBlocked` on safety rejections.
- FR-8.5: On `VeoSafetyBlocked` for beat[0] in premium mode, the stage falls back silently to the Imagen + Ken-Burns path for that beat. The original safety-blocked prompt is not written to any log output.
- FR-8.6: On `VeoQuotaExceeded`, the stage fails immediately. The operator reruns after quota resets.
- FR-8.7: `s06_video_assets` writes clips to `06_video_assets/beat_{00..03}.mp4`. Clips are validated via `ffprobe` to confirm codec and dimensions.
- FR-8.8: The `--no-veo` flag on `shipcast video_assets <slug>` forces the premium-mode beat[0] to use the Imagen + Ken-Burns path without calling Veo.

### Acceptance Criteria

- AC-8.1: Integration test (standard mode, mocked Gemini) asserts 4 MP4 clips in `06_video_assets/`, each 3–5 s (within ±0.1 s), 1080×1920, h264 codec.
- AC-8.2: Integration test (premium mode, mocked Veo) asserts beat[0] MP4 is 8.0 s ± 0.1 s and beats[1..3] are 3–5 s.
- AC-8.3: Fallback test: mocking `VeoSafetyBlocked` on beat[0] asserts a Ken-Burns clip is produced and the safety-blocked prompt does not appear in any log file.
- AC-8.4: Quota test: mocking `VeoQuotaExceeded` asserts stage fails with that exception and no clips are written for subsequent beats.

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/stages/s06_video_assets.py` | New |
| `src/shipcast/clients/veo_client.py` | New |
| `src/shipcast/clients/ffmpeg_client.py` | Extended — Ken-Burns helper, 1080×1920 preset, loop helper |
| `src/shipcast/clients/gemini_client.py` | Extended — `9:16` aspect ratio usage |

---

## 9. Stage 07: Voice Synthesis (s07_voice)

### Feature Description

`s07_voice` joins the narration lines from the storyboard beats into a single voiceover script, sends it to ElevenLabs for synthesis, and runs WhisperX to produce word-level timestamps for caption synchronization.

Maps to Slice 14.

### User Story

As an operator, I want a synthesized voiceover with word-level timestamps so that captions in the final video are precisely synchronized to the spoken narration.

### Functional Requirements

- FR-9.1: `s07_voice` joins `beat.narration` strings from `05_script/storyboard.json` with single newlines and sends the joined text to `elevenlabs_client.synthesize()`.
- FR-9.2: The voice ID used is read from `Settings.voice_id` (set in `config.toml`). The `voice.md` brand-pack file constrains the LLM script's tone but does not programmatically override `Settings.voice_id`.
- FR-9.3: The synthesized audio is saved to `07_voice/narration.mp3`.
- FR-9.4: `whisperx_client.transcribe(audio_path)` is called on the MP3 to produce `07_voice/words.json`, an array of `{word, start, end}` objects.
- FR-9.5: On ElevenLabs 429 quota error, the stage fails with `ElevenLabsQuotaExceeded`. The operator waits and reruns.

### Acceptance Criteria

- AC-9.1: Integration test asserts `07_voice/narration.mp3` exists and `ffprobe` reports a valid MP3 with duration matching the sum of `beat.duration_sec` values within ±1 s.
- AC-9.2: `07_voice/words.json` is non-empty and the sum of `(end - start)` for all word entries is within 1 s of the `ffprobe` MP3 duration.
- AC-9.3: ElevenLabs 429 mock asserts stage fails with `ElevenLabsQuotaExceeded` and no files are written.

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/stages/s07_voice.py` | New |
| `src/shipcast/clients/elevenlabs_client.py` | Extended — from the upstream scaffold, verified against v3 API |
| `src/shipcast/clients/whisperx_client.py` | Carried over from the upstream scaffold |

---

## 10. Stage 08: Video Assembly (s08_video)

### Feature Description

`s08_video` concatenates the video beat clips with the narration track, optionally mixes in background music from the brand pack, overlays synchronized captions, and exports both the full showcase video and a looping 6-second clip.

Maps to Slice 15.

### User Story

As an operator, I want a finished showcase video with captions and background music so that I can post it to Shorts, Reels, or X without any manual editing step.

### Functional Requirements

- FR-10.1: `s08_video._assemble_raw()` concatenates the four beat clips from `06_video_assets/`, mixes in `07_voice/narration.mp3` as the primary audio track, and optionally mixes in a background music track from `projects/_brand/<slug>/music/*.mp3` (first alphabetically). When bgm is present, narration is ducked to −3 dB relative to bgm.
- FR-10.2: `s08_video._overlay_captions()` burns captions using word timestamps from `07_voice/words.json`. Caption mode is read from the `caption_mode:` line in `03_brand/voice.md`. Recognized values: `chip`, `karaoke`, `reveal`. If the line is absent or the value is unrecognized, the default is `chip`.
- FR-10.3: Caption rendering is implemented in `src/shipcast/composition/captions.py`, adapted from the upstream scaffold subtitle-burn renderer to fit the 1080×1920 frame.
- FR-10.4: `s08_video._export_loop()` takes the first 6 s of beat[0] clip, center-crops it to 1080×1080, removes all audio, and exports both an MP4 and a GIF.
- FR-10.5: Output files written by `s08_video`:
  - `08_video/showcase.mp4` — full showcase video
  - `08_video/loop_6s.mp4` — 6-second looping clip (no audio)
  - `08_video/loop_6s.gif` — GIF version of the looping clip

### Acceptance Criteria

- AC-10.1: `08_video/showcase.mp4` passes `ffprobe -show_format`: duration 15.0–25.0 s (within ±0.5 s), video stream 1080×1920, codec h264, audio stream aac.
- AC-10.2: `08_video/loop_6s.mp4` passes `ffprobe`: duration 6.0 s ± 0.1 s, video stream 1080×1080, no audio stream.
- AC-10.3: `08_video/loop_6s.gif` file size ≤ 8 MB.
- AC-10.4: Visual-diff test asserts ≥ 95% of caption-region frames in `showcase.mp4` differ from the same frame in the raw assembled video (confirming captions were burned).
- AC-10.5: Caption-mode default test: running `s08_video` with an empty `voice.md` (no `caption_mode:` line) asserts the `chip` renderer was used.

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/stages/s08_video.py` | New |
| `src/shipcast/composition/captions.py` | New — adapted from the upstream scaffold subtitle-burn renderer |
| `src/shipcast/composition/layout.py` | New — `draw_outlined`, grid/padding helpers |
| `src/shipcast/clients/ffmpeg_client.py` | Extended — concat-mixed-input helper, loop/GIF export |

---

## 11. Stage 09: Static Graphics (s09_graphics)

### Feature Description

`s09_graphics` produces all static image assets in the marketing package: four aspect-ratio cards, an OG card for link-share previews, a conditional stat card, a conditional code screenshot, and the LinkedIn carousel.

Maps to Slices 16, 17, and 18.

### User Story

As an operator, I want a complete set of on-brand static graphics generated from the marketing brief so that I can attach the right format to each channel without manual design work.

### Functional Requirements

**Aspect-ratio cards**

- FR-11.1: `_render_aspect_card(ratio)` calls `gemini_client.generate_image(headline_prompt, aspect_ratio=ratio)` then overlays the brief's primary headline using `draw_outlined` with the brand display font from `03_brand/`. Four ratios are produced: `1x1`, `16x9`, `9x16`, `4x5`.
- FR-11.2: Each card's PIL image must pass the palette-conformance test: PIL `quantize(colors=5)` is applied; ≥ 80% of pixels must fall within ΔE-CIE2000 < 10 of one of the three brand colors (`primary`, `accent`, `neutral`) or pure white (#FFFFFF) or pure black (#000000).

**OG card**

- FR-11.3: `_render_og()` calls `gemini_client.generate_image(prompt, aspect_ratio="og")` (which produces a 1200×630 image) and overlays the entry name and brand logo.

**Stat card (conditional)**

- FR-11.4: `_render_stat(ratio)` is called for four ratios only when `brief.has_stat_card == true`. It produces `09_graphics/stat_{1x1,16x9,9x16,4x5}.png`.
- FR-11.5: When `brief.has_stat_card == false`, no `stat_*.png` files are created.

**Code screenshot (conditional)**

- FR-11.6: `_render_code()` is called only when `brief.has_code_screenshot == true`. It uses `src/shipcast/marketing/code_screenshot.py` (Pygments + PIL, Ray.so style — macOS chrome, syntax-highlighted, padded, drop-shadow) to produce `09_graphics/code.png`. No external API is called.
- FR-11.7: When `brief.has_code_screenshot == false`, no `code.png` file is created.

**LinkedIn carousel**

- FR-11.8: `_render_carousel_slide(idx, beat)` produces exactly 6 PNG slides at 1080×1350 pixels per slide, saved to `09_graphics/carousel/slide_{01..06}.png`:
  - Slide 01: hook headline (from `brief.hook_template_per_channel["linkedin"]`)
  - Slides 02–05: the four `brief.carousel_beats`
  - Slide 06: CTA
- FR-11.9: `src/shipcast/marketing/carousel.py` implements the carousel composer using PIL on brand-consistent templates.

### Acceptance Criteria

- AC-11.1: `09_graphics/` contains exactly `1x1.png`, `16x9.png`, `9x16.png`, `4x5.png`, `og_card.png`, and `carousel/slide_{01..06}.png` (6 files) for any run. Presence of `stat_*.png` and `code.png` depends on the brief flags.
- AC-11.2: PIL `Image.open(path).size` assertions:
  - `1x1.png` → (1080, 1080)
  - `16x9.png` → (1920, 1080)
  - `9x16.png` → (1080, 1920)
  - `4x5.png` → (1080, 1350)
  - `og_card.png` → (1200, 630)
  - Each `carousel/slide_*.png` → (1080, 1350)
- AC-11.3: Each of the four aspect-ratio cards passes the palette-conformance test (≥ 80% pixels within ΔE-CIE2000 < 10 of brand palette or white/black).
- AC-11.4: Stat-card test uses a pinned brief fixture with `has_stat_card=true` and asserts four `stat_*.png` files exist. Stat-card false test uses `has_stat_card=false` and asserts no `stat_*.png` files are created.
- AC-11.5: Code-screenshot test uses a pinned brief fixture with `has_code_screenshot=true` and asserts `code.png` exists and contains a valid syntax-highlighted block. False test asserts no `code.png` is created.
- AC-11.6: Carousel test asserts exactly 6 slide files, each (1080, 1350). Slide 01 contains the hook text (substring match). Slide 06 contains a CTA string.

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/stages/s09_graphics.py` | New |
| `src/shipcast/marketing/code_screenshot.py` | New — Pygments + PIL Ray.so renderer |
| `src/shipcast/marketing/carousel.py` | New — LinkedIn carousel composer |
| `src/shipcast/clients/gemini_client.py` | Extended — `"og"` aspect ratio (1200×630) |
| `src/shipcast/composition/layout.py` | New — shared PIL composition helpers |

---

## 12. Stage 10: Copy Generation (s10_copy)

### Feature Description

`s10_copy` produces the three text artifacts — X thread, LinkedIn long-form post, and Markdown blog post — via the `social-copywriter` sub-agent. Each artifact opens with the hook template chosen by the brief for that channel.

Maps to Slice 19.

### User Story

As an operator, I want publish-ready text copy for X, LinkedIn, and a blog, written in the brand voice and grounded in the changelog entry, so that I can paste the copy directly without rewriting.

### Functional Requirements

- FR-12.1: `s10_copy` invokes the `social-copywriter` sub-agent via `claude -p` (300 s timeout) with `04_plan/brief.json`, `01_pick/entry.json`, `02_enrich/context.json`, and `03_brand/voice.md` as context.
- FR-12.2: The sub-agent produces a JSON-serializable `CopyBundle` with fields `twitter_thread: str`, `linkedin: str`, and `blog: str`.
- FR-12.3: `s10_copy` writes three Markdown files:
  - `10_copy/twitter_thread.md` — 3 to 8 numbered tweets, each ≤ 280 characters.
  - `10_copy/linkedin.md` — 600 to 1200 words, valid CommonMark.
  - `10_copy/blog.md` — 1200 to 2000 words, valid CommonMark.
- FR-12.4: Each file's opening line uses the hook template that `brief.hook_template_per_channel` chose for that channel. Tests assert this by substring-matching the file's first non-blank line against the rendered output of `hooks.render(template_key, entry)`.
- FR-12.5: The X thread uses Unicode mathematical bold (e.g., `𝗯𝗼𝗹𝗱`) for emphasis (not Markdown bold). The LinkedIn post uses `→` or `▸` Unicode bullets (not Markdown `-`). Both constraints are enforced by the sub-agent prompt; tests assert absence of raw `**` Markdown bold in the Twitter file.
- FR-12.6: v1 produces a single draft per channel. A/B variants are deferred to v2.

### Acceptance Criteria

- AC-12.1: `10_copy/twitter_thread.md` contains 3–8 numbered tweets; no individual tweet exceeds 280 characters. Measured by splitting on numbered-tweet markers.
- AC-12.2: `10_copy/linkedin.md` word count is ≥ 600 and ≤ 1200. Valid CommonMark (passes `commonmark.js` or equivalent parse).
- AC-12.3: `10_copy/blog.md` word count is ≥ 1200 and ≤ 2000. Valid CommonMark.
- AC-12.4: Each file's first non-blank line matches a substring of `hooks.render(brief.hook_template_per_channel[channel], entry)`.
- AC-12.5: `social-copywriter` agent file exists at `~/.claude/agents/social-copywriter.md` with valid frontmatter.
- AC-12.6: Snapshot test in `tests/unit/test_subagents.py` mocks `claude -p` and asserts parsed output structure, field lengths, and absence of `**bold**` in the Twitter artifact.

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/stages/s10_copy.py` | New |
| `src/shipcast/schemas.py` | Extended — `CopyBundle` model |
| `~/.claude/agents/social-copywriter.md` | New — user-level sub-agent definition |

---

## 13. Stage 11: Package (s11_package)

### Feature Description

`s11_package` bundles all stage outputs into a `release.zip` and writes a `README.md` with one fenced code block per channel so the operator can copy-paste content directly from the README.

Maps to Slice 20.

### User Story

As an operator, I want a single ZIP archive and a paste-ready README so that I can hand the package to a team member or upload it without hunting across stage directories.

### Functional Requirements

- FR-13.1: `s11_package` writes `11_package/release.zip` containing all output files from stages 01–10.
- FR-13.2: `s11_package` writes `11_package/README.md` with:
  - A Markdown table listing every asset with its filename, dimensions (where applicable), and intended channel.
  - A fenced code block for each of the three text artifacts (tweets, LinkedIn post, blog post).
- FR-13.3: The ZIP listing must include at minimum: `08_video/showcase.mp4`, `08_video/loop_6s.mp4`, `09_graphics/1x1.png`, `09_graphics/16x9.png`, `09_graphics/9x16.png`, `09_graphics/4x5.png`, `09_graphics/og_card.png`, `09_graphics/carousel/slide_{01..06}.png`, `10_copy/twitter_thread.md`, `10_copy/linkedin.md`, `10_copy/blog.md`.
- FR-13.4: Conditional files (`09_graphics/stat_*.png`, `09_graphics/code.png`) are included in the ZIP when present.
- FR-13.5: `s11_package` invokes the `code-reviewer` sub-agent via `claude -p` (300 s timeout) for a sanity check on README links. On timeout or parse failure, the stage fails.

### Acceptance Criteria

- AC-13.1: `unzip -l 11_package/release.zip` output matches the expected file set (all required files, plus conditional files when flags were set in the brief).
- AC-13.2: `11_package/README.md` contains at least three fenced code blocks (one per text channel) and a Markdown table with at least nine rows (one per output asset).
- AC-13.3: Re-running `s11_package` on the same inputs produces a byte-identical `release.zip` (idempotency, controlled by sorting entries before zip assembly).

### Affected Components

| Path | Change |
|------|--------|
| `src/shipcast/stages/s11_package.py` | New |
| `src/shipcast/schemas.py` | Extended — `PackageManifest` model |

---

## 14. Marketing Strategy Constraints

### Feature Description

This section documents the marketing rules that constrain what every creative stage produces. These are not implementation details — they are business requirements that the `brand-guardian` sub-agent enforces and that `social-copywriter` must follow.

### Hook Template Catalog

`src/shipcast/marketing/hooks.py` defines a fixed catalog of seven templates. `s04_plan` selects one per channel and bakes the choice into `MarketingBrief.hook_template_per_channel`. The catalog is fixed; the `planner` sub-agent may not invent new templates.

| Template key | When to use | Example opening |
|---|---|---|
| `we_just_shipped` | Default for any feature ship | "We just shipped X." |
| `before_after` | UX or speed improvement | "Yesterday: 3 clicks. Today: 1." |
| `problem_aha` | Bug fix or pain-point relief | "If you've ever lost 20 minutes to \<pain\>, this fixes it." |
| `numbered_list` | Multi-change releases | "3 things we built this week." |
| `behind_the_scenes` | Architectural or refactor wins | "Why we rewrote our X." |
| `5_sec_value` | Small but high-leverage change | "X now does Y in one click." |
| `social_proof` | Feature inspired by user request | "\<@user\> asked. We built." |

### Channel-Specific Anatomy

**X thread (3–8 tweets):**
- Tweet 1: hook + 1 visual (showcase video, 16:9 graphic, or code screenshot). ≤ 200 characters (leaves room for visual preview).
- Tweets 2 to N−1: numbered points, one idea per tweet, ≤ 200 characters. Bold key terms with Unicode mathematical bold.
- Final tweet: CTA + "if this helped, RT the first tweet."

**LinkedIn (600–1200 words):**
- First line: hook (≤ 200 characters visible before "see more").
- Single blank line after hook.
- 3–5 short paragraphs (2–3 sentences each) with generous line breaks.
- Numbered or bulleted middle section using `→` or `▸` Unicode, not Markdown.
- Closing line: a question or invitation to comment.
- 3–5 lowercase hashtags at end of post.

**Blog (1200–2000 words):**
- TL;DR block (≤ 100 words, 3–5 bullets) at the top.
- Narrative arc: problem → constraint → exploration → solution → result.
- 2–4 visuals embedded: at least 1 real-app screenshot, optionally 1 code screenshot.
- Code samples in fenced blocks with language tag.
- Closing CTA: "try it" link + "feedback welcome" + author byline.

### Visual Style Contract

**Typography:**
- FR-14.1: Maximum two fonts. Default if the brand pack omits a font: Inter (display and body). Arial and Impact are banned defaults (look AI-generated).
- FR-14.2: Brand pack must supply at least one `.ttf` in `_brand/<slug>/fonts/`. Stage `s03_brand` raises `BrandPackIncomplete` if absent.

**Color:**
- FR-14.3: After operator edit of `proposal.json`, exactly three hex codes are used: `primary` (headlines, CTA buttons), `accent` (secondary highlights), `neutral` (body text, backgrounds).
- FR-14.4: 60-30-10 rule: 60% neutral, 30% primary, 10% accent — measured on the quantized-to-5 PIL palette histogram, with ±15% tolerance per channel.

**Palette conformance test:**
- FR-14.5: Each generated graphic is PIL-`quantize(colors=5)` reduced, then ≥ 80% of its pixels must fall within ΔE-CIE2000 < 10 of one of: brand `primary`, `accent`, `neutral`, pure white (#FFFFFF), or pure black (#000000). This tolerates anti-aliasing and intermediate Gemini shades while still rejecting obviously off-palette outputs. The test helper lives in `tests/unit/test_palette_conformance.py` and is called by all graphics integration tests.

**Spacing:**
- FR-14.6: 8-point grid. All static graphics have ≥ 8% padding on all sides.

**Motion:**
- FR-14.7: Default motion language is slow push-in / smooth transitions. Hyperactive cuts are banned unless `voice.md` explicitly includes the term "energetic" or "tiktok-native". Veo 3 prompts in premium mode include the motion-style modifier from `voice.md`.

**Caption animation:**
- FR-14.8: Three modes supported: `chip` (default, palette-paired subtitle chips), `karaoke` (word-by-word highlight), `reveal` (text fades/scales in per chip). Mode is set by the `caption_mode:` line in `voice.md`. Default is `chip` when the line is absent or unrecognized.

### Acceptance Criteria

- AC-14.1: Catalog unit test: all seven `hooks.render(key, sample_entry)` calls return non-empty strings.
- AC-14.2: Palette-conformance helper `test_palette_conformance.py` passes for all four aspect-ratio cards from a test run using a pinned brand fixture.
- AC-14.3: Caption-mode test: stage `s08_video` run with `voice.md` containing `caption_mode: karaoke` uses the karaoke renderer; run with empty `voice.md` uses the chip renderer.

---

## 15. Two Video Modes

### Feature Description

The operator selects a video mode per project in `input.yaml`. Standard mode is the default for routine launches and stays within a $3 cost cap. Premium mode adds a Veo 3 Fast native-motion hero clip for marquee launches, within an $8 cost cap.

### Mode Comparison Table

| Attribute | `standard` (default) | `premium` (opt-in) |
|---|---|---|
| Hero shot (beat[0]) | Gemini Imagen still + Ken-Burns pan/zoom | Gemini Veo 3 Fast × 1 (8 s native motion) |
| Fill shots (beats[1..3]) | Gemini Imagen stills + Ken-Burns | Gemini Imagen stills + Ken-Burns |
| Per-entry estimated cost | ~$0.83 ($0.64 Imagen + $0.04 multimodal + $0.15 ElevenLabs) | ~$4.03 (+$3.20 Veo 3 Fast) |
| Cost cap | $3.00 | $8.00 |
| Wall-clock (excluding gate waits) | ≤ 30 minutes | ≤ 45 minutes |
| Veo client invoked? | Never | Yes, for beat[0] only |
| Safety-block fallback | N/A | Ken-Burns for beat[0] |

### Functional Requirements

- FR-15.1: `input.yaml.video_mode` accepts exactly `"standard"` or `"premium"`. Any other value is a validation error.
- FR-15.2: `Settings.max_cost_usd_per_project` is set to 3.00 for standard mode and 8.00 for premium mode, derived from `input.yaml.video_mode` at project creation.
- FR-15.3: Full Veo 3 (non-Fast) is not supported in v1. Any attempt to configure it raises `UnsupportedVideoMode`.
- FR-15.4: The `--no-veo` flag on `shipcast video_assets <slug>` forces premium-mode beat[0] to use the Imagen + Ken-Burns path without calling Veo, without changing the mode setting in `input.yaml`.

### Acceptance Criteria

- AC-15.1: Standard-mode pipeline mock test: `tests/integration/test_cost_cap.py` asserts accumulated cost ≤ $3.00 across all stages.
- AC-15.2: Premium-mode pipeline mock test: asserts accumulated cost ≤ $8.00.
- AC-15.3: Cost-cap enforcement test: mocking a stage call that would push cost just past the cap asserts `CostCapExceeded` is raised before the call is made.
- AC-15.4: `--no-veo` flag test: mocking `veo_client.generate_clip` to raise `AssertionError` and running `shipcast video_assets <slug> --no-veo` asserts the mock is never called and Ken-Burns clips are produced for all four beats.

---

## 16. Non-Functional Requirements

- NFR-16.1: **Language and tooling.** Python 3.12+, managed with `uv`. `uv run mypy --strict src/shipcast` must report zero errors. `uv run ruff check src tests` must report zero findings.
- NFR-16.2: **Test coverage.** `src/shipcast/manifest.py` must achieve 100% line and branch coverage. Package-wide coverage must be ≥ 90% (`pytest --cov=shipcast --cov-fail-under=90`). Coverage uses branch tracking (`branch = true` in `pyproject.toml`).
- NFR-16.3: **Platform.** macOS and Linux only. `fcntl.flock` is the concurrency primitive. Running on any other platform raises `UnsupportedPlatform` at CLI startup.
- NFR-16.4: **Cost caps.** Standard mode: ≤ $3.00 per entry. Premium mode: ≤ $8.00 per entry. Caps are enforced at runtime by the dispatcher before each paid API call (see FR-1.28).
- NFR-16.5: **Wall-clock performance.** Total run time per entry, excluding human gate wait time: ≤ 30 minutes in standard mode, ≤ 45 minutes in premium mode (Veo 3 Fast polling adds ~2–3 min per clip; Imagen ~5–10 s per image × 16 images; ElevenLabs ~30 s; WhisperX ~30 s; ffmpeg encoding ~2 min).
- NFR-16.6: **Idempotency.** Deterministic stages (parsing, schema validation, composition) must produce byte-identical output files on re-run of identical inputs. LLM and AI stages need not be byte-deterministic, but non-determinism must be confined to the `run()` call body (no `datetime.now()` in artifact JSON, no random `id` fields).
- NFR-16.7: **Atomicity.** Manifest writes use the temp-file + `os.replace` pattern. A mid-write crash leaves `manifest.json` unmodified.
- NFR-16.8: **Security.** API keys live in `.env` (gitignored). `.env.example` contains key names only (empty values). `SecretStr` fields are never serialized into `config_snapshot` or log output. `live_url` is validated before any Playwright navigation (SSRF defence). `repo_path` is validated before any filesystem access (path-traversal defence).
- NFR-16.9: **Sub-agent timeouts.** All `claude -p` sub-agent invocations use a 300-second timeout. Timeout and JSON parse failures produce named exception classes recorded in `manifest.stages[id].error`.
- NFR-16.10: **Startup time.** `shipcast --help` and `shipcast status` must complete in under 1 second (no heavy imports at CLI startup; clients are lazy).

---

## 17. Out of Scope for v1

The following are explicitly deferred to v2 and must not be implemented during the v1 build:

- **Full Playwright-driven demo-capture:** multi-step live-app walkthroughs with synchronized voiceover per step. v1 ships enrichment screenshots only (single-page snapshots).
- **Auto-publishing:** no X, LinkedIn, or dev.to posting. v1 produces copy that the operator pastes manually.
- **`shipcast watch` poller / git-hook trigger:** v1 is entirely operator-driven.
- **Multi-language brand voice:** all copy is produced in English only.
- **Per-channel A/B variant generation:** v1 produces a single draft per channel. A/B variants were considered and explicitly declined.
- **Talking-head founder cameo intros:** no HeyGen or Synthesia integration.
- **Suno API integration for bgm generation:** v1 expects operator-supplied or hand-picked tracks in `_brand/<slug>/music/`.
- **Ideogram API for text-in-image:** v1 uses local PIL text overlay on Gemini backgrounds.
- **Analytics ingestion:** no YouTube Data API, X engagement, or LinkedIn impressions collection.
- **Self-healing Playwright selectors:** no `e2e-runner` sub-agent for selector repair.
- **Animated graphics (Rive/Lottie):** v1 produces static PNGs only.
- **Full Veo 3 (non-Fast):** too expensive for routine use at current pricing. Revisit in v2.
- **`shipcast watch` / git-hook automation:** v1 is manual-trigger only.
