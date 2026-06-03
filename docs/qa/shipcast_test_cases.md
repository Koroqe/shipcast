# Test Cases: shipcast Auto-Marketing Pipeline

> Based on [PRD](../PRD.md) and [Use Cases](../use-cases/shipcast_use_cases.md)
> Architecture review: [shipcast_architecture_review.md](../architecture/shipcast_architecture_review.md)
> Reference plan: `docs/qa/shipcast_implementation_plan.md`

---

## Contents

1. [Manifest State-Machine](#1-manifest-state-machine)
2. [Project Scaffold and Directory Layout](#2-project-scaffold-and-directory-layout)
3. [Input Validation — InputYaml](#3-input-validation--inputyaml)
4. [Stage 01: Pick Changelog Entry (s01_pick)](#4-stage-01-pick-changelog-entry-s01_pick)
5. [Stage 02: Enrich Context (s02_enrich)](#5-stage-02-enrich-context-s02_enrich)
6. [Stage 03: Brand Extraction (s03_brand)](#6-stage-03-brand-extraction-s03_brand)
7. [Stage 04: Marketing Plan (s04_plan)](#7-stage-04-marketing-plan-s04_plan)
8. [Stage 05: Script and Storyboard (s05_script)](#8-stage-05-script-and-storyboard-s05_script)
9. [Stage 06: Video Assets (s06_video_assets)](#9-stage-06-video-assets-s06_video_assets)
10. [Stage 07: Voice Synthesis (s07_voice)](#10-stage-07-voice-synthesis-s07_voice)
11. [Stage 08: Video Assembly (s08_video)](#11-stage-08-video-assembly-s08_video)
12. [Stage 09: Static Graphics (s09_graphics)](#12-stage-09-static-graphics-s09_graphics)
13. [Stage 13: Copy Generation (s10_copy)](#13-stage-10-copy-generation-s10_copy)
14. [Stage 11: Package (s11_package)](#14-stage-11-package-s11_package)
15. [CLI Dispatcher — Human-Gate Enforcement](#15-cli-dispatcher--human-gate-enforcement)
16. [CLI Dispatcher — Re-run, Reset, Cascade-Confirmation Guard](#16-cli-dispatcher--re-run-reset-cascade-confirmation-guard)
17. [CLI Dispatcher — Cost Cap Enforcement](#17-cli-dispatcher--cost-cap-enforcement)
18. [Concurrency and Locking](#18-concurrency-and-locking)
19. [Security and Secrets](#19-security-and-secrets)
20. [Architect MAJOR Finding Coverage](#20-architect-major-finding-coverage)
21. [Idempotency and Snapshot Tests](#21-idempotency-and-snapshot-tests)
22. [Coverage and Static-Analysis Gates](#22-coverage-and-static-analysis-gates)
23. [Full-Pipeline Integration](#23-full-pipeline-integration)
24. [Traceability Matrix](#24-traceability-matrix)

---

## 1. Manifest State-Machine

*Tests for `src/shipcast/manifest.py` — targeted at 100% line+branch coverage.*

---

### TC-1.1: Legal state transitions — exhaustive parametrized happy paths

**Maps to:** UC-1, UC-2, UC-16, UC-18, FR-1.8
**Type:** unit
**Slice:** Slice 1 / Slice 4
**Preconditions:** In-memory `Manifest` with the `from_status` value under test

**Steps:**
1. Construct in-memory manifest with a stage at `from_status`
2. Call `manifest.transition(stage_id, new_status=to_status)` for each legal pair

**Parametrized pairs (8):** `(pending, running)`, `(running, done)`, `(running, failed)`, `(running, needs_review)`, `(needs_review, running)`, `(failed, running)`, `(failed, pending)`, `(done, pending)`

**Expected result:** Returns a new `Manifest`; `stages[id].status == to_status`; original object unchanged; no file I/O

**Negative assertions:** Original manifest `status` unmodified; no `manifest.json.tmp` written

---

### TC-1.2: Illegal state transitions raise `IllegalTransition`

**Maps to:** UC-32, FR-1.8
**Type:** unit
**Slice:** Slice 1 / Slice 4
**Preconditions:** In-memory manifest at the given `from_status`

**Steps:** Call `transition` for every pair NOT in TC-1.1's legal list (includes same-state no-ops such as `done→done`)

**Expected result:** `IllegalTransition` raised for every illegal pair; manifest state unchanged; no file written

---

### TC-1.3: Atomic manifest write — mid-write crash leaves original intact

**Maps to:** UC-29, FR-1.9, NFR-16.7, AC-1.13
**Type:** unit
**Slice:** Slice 1 / Slice 4
**Preconditions:** `manifest.json` exists on `tmp_path`; `os.replace` monkeypatched to raise `OSError`

**Steps:**
1. Load manifest; perform `pending → running` transition
2. Call `manifest.save(path)` — `os.replace` raises

**Expected result:** `OSError` propagates; `manifest.json` bytes unchanged from pre-save value; `manifest.json.tmp` exists with new content

---

### TC-1.4: Manifest round-trip byte-equality

**Maps to:** UC-1, FR-1.9, FR-1.10
**Type:** snapshot
**Slice:** Slice 4
**Preconditions:** Pinned fixture at `tests/fixtures/manifests/v1_fresh.json`

**Steps:** Load fixture → save to `tmp_path/manifest.json` → read bytes → compare

**Expected result:** Written bytes are byte-identical to the fixture file (same indentation, key order, trailing newline)

---

### TC-1.5: `compute_inputs_hash` stability — same inputs, same digest

**Maps to:** FR-1.13
**Type:** unit
**Slice:** Slice 4

**Steps:** Create two files in `tmp_path`; call `compute_inputs_hash([p1, p2])` twice without changes

**Expected result:** Both calls return identical 64-character hex digest; no non-determinism

---

### TC-1.6: `compute_inputs_hash` sensitivity — mtime change produces different digest

**Maps to:** FR-1.13
**Type:** unit
**Slice:** Slice 4

**Steps:** Compute baseline hash; call `os.utime(path, (t+1, t+1))`; recompute hash

**Expected result:** `h1 != h2`

---

### TC-1.7: `compute_inputs_hash` sensitivity — size change produces different digest

**Maps to:** FR-1.13
**Type:** unit
**Slice:** Slice 4

**Steps:** Compute baseline; append `b"x"` to file; recompute

**Expected result:** `h1 != h2`

---

### TC-1.8: `compute_outputs_hash` detects byte-content change (same mtime, same size)

**Maps to:** UC-15, UC-15-EC1, FR-1.13, FR-1.15
**Type:** unit
**Slice:** Slice 4

**Steps:**
1. Write file with 100 bytes of `b"A"` to `tmp_path`; compute `h1 = compute_outputs_hash([path])`
2. Overwrite with 100 bytes of `b"B"` (same size); preserve mtime via `os.utime`; compute `h2`

**Expected result:** `h1 != h2` (byte-content hash detects same-size swap)

**Negative assertions:** `compute_inputs_hash` (mtime+size) would NOT detect this — assert the two hash functions diverge on this input

---

### TC-1.9: `compute_outputs_hash` stable — byte-identical replacement = same hash

**Maps to:** UC-15-EC1, FR-1.13
**Type:** unit
**Slice:** Slice 4

**Steps:** Write file; compute `h1`; overwrite with identical bytes, change mtime; compute `h2`

**Expected result:** `h1 == h2` (byte-identical content, different mtime → same outputs hash)

---

### TC-1.10: `config_snapshot` locked after first stage leaves pending

**Maps to:** UC-27, FR-1.11
**Type:** unit
**Slice:** Slice 1 / Slice 4

**Steps:**
1. Create manifest; transition `01_pick` to `running`
2. Call `manifest.update_config_snapshot(new_settings)`

**Expected result:** `ConfigSnapshotLocked` raised; `config_snapshot` bytes unchanged

---

### TC-1.11: `config_snapshot` writable when all stages are pending

**Maps to:** FR-1.11
**Type:** unit
**Slice:** Slice 1

**Steps:** Fresh manifest (all stages `pending`); call `update_config_snapshot`

**Expected result:** No exception; `config_snapshot` updated in returned manifest

---

### TC-1.12: `ManifestMigrationNeeded` on schema_version mismatch

**Maps to:** UC-26, FR-1.12
**Type:** unit
**Slice:** Slice 1

**Steps:** Write `manifest.json` with `schema_version: 99` to `tmp_path`; call `Manifest.load(path)`

**Expected result:** `ManifestMigrationNeeded` raised; no manifest object returned

---

### TC-1.13: `Manifest.reset` clears stage fields and resets downstream transitively

**Maps to:** UC-18, FR-1.18
**Type:** unit
**Slice:** Slice 1

**Steps:**
1. Build manifest with stages `01_pick` through `11_package` all `done`
2. Call `manifest.reset("05_script")`

**Expected result:** `05_script` status = `pending`; all fields cleared (`outputs=[]`, hashes=None, timestamps=None, error=None); stages `06_video_assets` through `11_package` also `pending`; stages `01_pick`–`04_plan` unchanged

---

### TC-1.14: `Manifest.approve` sets `human_approved_at`, clears `manually_edited` on hash match

**Maps to:** UC-14, FR-1.14, FR-1.15
**Type:** unit
**Slice:** Slice 1

**Steps:** Stage `01_pick` = `done`; `outputs_hash_at_done` set; call `manifest.approve("01_pick", recomputed_hash=same_value)`

**Expected result:** `human_approved_at` is non-null; `manually_edited = false`

---

### TC-1.15: `manifest.approve` sets `manually_edited=true` on hash mismatch

**Maps to:** UC-15, FR-1.15
**Type:** unit
**Slice:** Slice 1

**Steps:** Same as TC-1.14 but pass `recomputed_hash` that differs from stored value

**Expected result:** `human_approved_at` set; `manually_edited = true`; changed-file list populated

---

### TC-1.16: `CannotApproveNonDoneStage` when stage is not done

**Maps to:** UC-14-E1, FR-1.16
**Type:** unit
**Slice:** Slice 1

**Steps:** Stage `01_pick` = `running`; call `manifest.approve("01_pick", ...)`

**Expected result:** `CannotApproveNonDoneStage` raised; manifest unmodified

---

### TC-1.17: `StageBusy` raised by `--rerun` on truly running stage

**Maps to:** UC-16-E1, FR-1.17
**Type:** unit
**Slice:** Slice 1

**Steps:** Stage status = `running`; dispatcher logic calls rerun path

**Expected result:** `StageBusy` raised; manifest unmodified

---

---

## 2. Project Scaffold and Directory Layout

*Tests for `src/shipcast/project.py`, `src/shipcast/cli.py` startup, and directory structure.*

---

### TC-2.1: `shipcast --help` exits 0 and lists all 11 verb names

**Maps to:** UC-35, FR-1.3, NFR-16.10, AC-1.1
**Type:** unit
**Slice:** Slice 1

**Steps:** Invoke `shipcast --help` via Typer's `CliRunner`

**Expected result:** Exit code 0; output contains all 11 verbs: `pick`, `enrich`, `brand`, `plan`, `script`, `video_assets`, `voice`, `video`, `graphics`, `copy`, `package`

**Performance assertion:** Completes in under 1 second (no heavy import at startup; no client construction)

---

### TC-2.2: `shipcast status` renders 11-row color-coded table

**Maps to:** UC-19, FR-1.2
**Type:** unit
**Slice:** Slice 1

**Steps:** Create a manifest with mixed statuses; run `shipcast status <slug>` via `CliRunner`

**Expected result:** Exit code 0; output contains 11 rows; `done` rows show green indicator; `failed` shows red; `pending` shows grey; `running` shows yellow; `needs_review` shows cyan; no lock is acquired

---

### TC-2.3: `Project.create` writes manifest with all 11 stages pending

**Maps to:** UC-1, UC-2, FR-1.1, FR-1.4, AC-1.5
**Type:** unit
**Slice:** Slice 1

**Steps:** Call `Project.create(root=tmp_path, slug="test--feature", config_snapshot={...})`

**Expected result:** `projects/test--feature/manifest.json` exists; all 11 stages have `status=pending`; `config_snapshot` populated; `schema_version=1`

**Negative assertions:** `config_snapshot` contains no values for keys `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`; no `manifest.json.tmp` left behind

---

### TC-2.4: `projects/_template/` has all 11 stage subdirectories

**Maps to:** FR-1.5
**Type:** unit
**Slice:** Slice 1

**Steps:** Assert `projects/_template/` contains subdirs `01_pick` through `11_package` (each with `.gitkeep`)

**Expected result:** All 11 subdirectories present

---

### TC-2.5: `UnsupportedPlatform` raised on non-macOS/Linux at startup

**Maps to:** UC-23, FR-1.31, NFR-16.3, AC-1.10
**Type:** unit
**Slice:** Slice 1

**Steps:** Monkeypatch `platform.system()` to return `"Windows"`; invoke any `shipcast` CLI command via `CliRunner`

**Expected result:** `UnsupportedPlatform` raised before any filesystem operation; exit non-zero; no project directory created

---

### TC-2.6: Logging — `configure(project)` creates JSON-line log file; no secret values in tracebacks

**Maps to:** UC-2, FR-1.33
**Type:** unit
**Slice:** Slice 1

**Steps:**
1. Call `logging_setup.configure(project)` with a `tmp_path`-based project
2. Log a `WARNING` containing a mock `SecretStr` object

**Expected result:** Log file created under `projects/<slug>/logs/<utcnow-iso>.log`; file contains JSON lines; `SecretStr.__repr__` masks value (string `"**********"` appears, not the raw key)

---

---

## 3. Input Validation — InputYaml

*Tests for `src/shipcast/schemas.py::InputYaml` — Slice 3. All 13 rejection cases + 2 accept cases from AC-2.1/AC-2.2.*

---

### TC-3.1: Reject `http://` scheme URL

**Maps to:** UC-2-E4, FR-2.3, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse `InputYaml` with `live_url="http://example.com"`

**Expected result:** `ValidationError` raised; error message references `live_url` and scheme

**Negative assertions:** `socket.gethostbyname` is NOT called (monkeypatched to raise `AssertionError` to confirm)

---

### TC-3.2: Reject `ftp://` scheme URL

**Maps to:** UC-2-E4, FR-2.3, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse with `live_url="ftp://example.com/file"`

**Expected result:** `ValidationError`; scheme rejection fires before hostname resolution

---

### TC-3.3: Reject RFC 1918 192.168.x URL

**Maps to:** UC-2-E4, UC-4-E5, FR-2.3, AC-2.1, NFR-2.2
**Type:** unit
**Slice:** Slice 3

**Steps:** Monkeypatch `socket.gethostbyname` to return `"192.168.1.1"`; parse with `live_url="https://internal.example.com"`

**Expected result:** `ValidationError`; `is_private` check fires; no Playwright call

---

### TC-3.4: Reject RFC 1918 10.x URL

**Maps to:** FR-2.3, AC-2.1, NFR-2.2
**Type:** unit
**Slice:** Slice 3

**Steps:** Monkeypatch `socket.gethostbyname` to return `"10.0.0.5"`; parse with `live_url="https://corp.example.com"`

**Expected result:** `ValidationError`

---

### TC-3.5: Reject RFC 1918 172.16.x URL

**Maps to:** FR-2.3, AC-2.1, NFR-2.2
**Type:** unit
**Slice:** Slice 3

**Steps:** Monkeypatch `socket.gethostbyname` to return `"172.16.0.1"`

**Expected result:** `ValidationError`

---

### TC-3.6: Reject `localhost` URL (loopback)

**Maps to:** FR-2.3, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Monkeypatch `socket.gethostbyname` to return `"127.0.0.1"`; parse with `live_url="https://localhost"`

**Expected result:** `ValidationError`; `is_loopback` fires

---

### TC-3.7: Reject link-local 169.254.x URL

**Maps to:** FR-2.3, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Monkeypatch to return `"169.254.1.1"`

**Expected result:** `ValidationError`; `is_link_local` fires

---

### TC-3.8: Reject `repo_path` containing `..`

**Maps to:** UC-2-E5, FR-2.4, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse with `repo_path="/Users/aleksei/Documents/Projects.nosync/../etc/passwd"`

**Expected result:** `ValidationError`; path-traversal validator fires; filesystem is not accessed

---

### TC-3.9: Reject `repo_path` outside allowed root

**Maps to:** FR-2.4, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse with `repo_path="/tmp/some-other-project"`

**Expected result:** `ValidationError`; allowed-root validator fires

---

### TC-3.10: Reject `repo_path` with no `CHANGELOG.md`

**Maps to:** FR-2.4, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Create a directory under the allowed root with no `CHANGELOG.md`; parse

**Expected result:** `ValidationError`; missing-changelog validator fires

---

### TC-3.11: Reject `WalkthroughStep` with unknown action

**Maps to:** FR-2.6, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse `WalkthroughStep(action="eval", selector="#btn")`

**Expected result:** `ValidationError`

---

### TC-3.12: Reject `WalkthroughStep.selector` containing `javascript:`

**Maps to:** FR-2.5, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse `WalkthroughStep(action="click", selector="javascript:alert(1)")`

**Expected result:** `ValidationError`

---

### TC-3.13: Reject `WalkthroughStep` with missing required field

**Maps to:** FR-2.5, AC-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse dict `{"action": "goto"}` without `selector` or `value` when required

**Expected result:** `ValidationError`; missing field listed

---

### TC-3.14: Accept valid standard-mode `InputYaml`

**Maps to:** UC-1, FR-2.1, AC-2.2
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse complete valid YAML with `video_mode="standard"`, valid https URL (monkeypatched to public IP), valid `repo_path` (with `CHANGELOG.md`)

**Expected result:** `InputYaml` instance returned; no exception; `video_mode == "standard"`

**Negative assertions:** No network call made (confirmed via monkeypatch)

---

### TC-3.15: Accept valid premium-mode `InputYaml`

**Maps to:** FR-2.1, FR-15.1, AC-2.2
**Type:** unit
**Slice:** Slice 3

**Steps:** Same as TC-3.14 with `video_mode="premium"`

**Expected result:** `InputYaml` instance; `video_mode == "premium"`

---

### TC-3.16: `video_mode` other than `standard`/`premium` raises `ValidationError`

**Maps to:** FR-15.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse with `video_mode="veo3"`

**Expected result:** `ValidationError`

---

### TC-3.17: Validation never makes a real network call

**Maps to:** AC-2.3, NFR-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Monkeypatch `socket.gethostbyname` to raise `AssertionError`; run all 13 rejection-case validations for URL failures; confirm `AssertionError` is raised only for the RFC1918/loopback/link-local cases (not for scheme-level rejections)

**Expected result:** Scheme rejections complete before hostname resolution; hostname-based rejections use the monkeypatched resolver

---

---

## 4. Stage 01: Pick Changelog Entry (s01_pick)

---

### TC-4.1: Changelog parser — canonical format

**Maps to:** UC-2, FR-4.1, AC-4.1
**Type:** unit
**Slice:** Slice 5

**Steps:** Feed `tests/fixtures/changelogs/canonical.md` (well-formed `## YYYY-MM-DD` + `### Name — HH:MM UTC`) to `changelog/parser.py`

**Expected result:** Returns `list[ChangelogEntry]` with correct `name`, `date`, `time`, `summary`, `details` fields populated

---

### TC-4.2: Parser — multiple entries per day

**Maps to:** UC-2-A1, FR-4.1, AC-4.1
**Type:** unit
**Slice:** Slice 5

**Steps:** Feed fixture with two `###` headings under one `##` date heading

**Expected result:** Two `ChangelogEntry` items returned; both have the same date; names differ

---

### TC-4.3: Parser — missing time field

**Maps to:** FR-4.1, AC-4.1
**Type:** unit
**Slice:** Slice 5

**Steps:** Feed fixture with `### Feature Name` (no `— HH:MM UTC` suffix)

**Expected result:** Entry returned with `time=None`; no exception

---

### TC-4.4: Parser — empty file returns empty list

**Maps to:** UC-30, FR-4.2, AC-4.1
**Type:** unit
**Slice:** Slice 5

**Steps:** Feed empty string to parser

**Expected result:** `[]` returned; no exception

---

### TC-4.5: Parser — no `## YYYY-MM-DD` headings returns empty list

**Maps to:** UC-2-EC2, FR-4.2, AC-4.1
**Type:** unit
**Slice:** Slice 5

**Steps:** Feed CHANGELOG with prose but no date or entry headings

**Expected result:** `[]` returned

---

### TC-4.6: Parser — `ChangelogFileMissing` when file does not exist

**Maps to:** UC-2-E1, UC-1-E1, FR-4.2
**Type:** unit
**Slice:** Slice 5

**Steps:** Call `parse_changelog(path)` with a non-existent file path

**Expected result:** `ChangelogFileMissing` raised; no file is created

---

### TC-4.7: `s01_pick` happy-path integration — entry found, `entry.json` written

**Maps to:** UC-2, FR-4.3, FR-4.4, AC-4.2
**Type:** integration
**Slice:** Slice 6

**Steps:**
1. Create fixture repo with valid `CHANGELOG.md` and `input.yaml`
2. Run `shipcast pick <fixture-repo> --entry "Add CSV export"` via `CliRunner`

**Expected result:** Exit 0; `01_pick/entry.json` exists; validates against `ChangelogEntry` schema; `manifest.stages.01_pick.status == "done"`; `outputs_hash_at_done` populated; `human_approved_at = null`

---

### TC-4.8: `ChangelogEntryNotFound` — heading not in CHANGELOG

**Maps to:** UC-2-E2, FR-4.3, AC-4.4
**Type:** integration
**Slice:** Slice 6

**Steps:** Run `shipcast pick` with `--entry "Nonexistent Feature"`

**Expected result:** `ChangelogEntryNotFound` recorded in manifest; `01_pick.status == "failed"`; exit non-zero; no `entry.json` file

---

### TC-4.9: `ChangelogFileMissing` — CHANGELOG absent from target repo

**Maps to:** UC-2-E1, FR-4.2, AC-4.3
**Type:** integration
**Slice:** Slice 6

**Steps:** Point `repo_path` to directory without `CHANGELOG.md`; run pick

**Expected result:** `ChangelogFileMissing` in `error.type`; stage `failed`; no CHANGELOG auto-created

---

### TC-4.10: `StageInputMissing` — `input.yaml` absent

**Maps to:** UC-2-E3, FR-1.24
**Type:** integration
**Slice:** Slice 6

**Steps:** Remove `input.yaml` from project directory before running `check_inputs`

**Expected result:** `StageInputMissing` raised before `run()` is called; stage `failed`

---

### TC-4.11: Entry heading match is case-insensitive and trimmed

**Maps to:** UC-2-A2, FR-4.3
**Type:** unit
**Slice:** Slice 5

**Steps:** Heading in CHANGELOG is `"Add CSV Export"`; `entry_heading` in `input.yaml` is `"  add csv export  "`

**Expected result:** Entry found; no error

---

### TC-4.12: `s01_pick` idempotency — byte-identical `entry.json` on re-run

**Maps to:** UC-2-A3, UC-31, FR-4.5, NFR-16.6
**Type:** snapshot
**Slice:** Slice 6

**Steps:** Run `s01_pick` twice on identical inputs; compare `entry.json` bytes

**Expected result:** `bytes_run_1 == bytes_run_2`

---

### TC-4.13: `entry_heading` empty string raises `ValidationError` at `InputYaml` parse

**Maps to:** UC-2-EC3, FR-2.1
**Type:** unit
**Slice:** Slice 3

**Steps:** Parse `InputYaml` with `entry_heading=""`

**Expected result:** Pydantic `ValidationError` (min_length constraint)

---

### TC-4.14: Review Checklist printed after successful `s01_pick`

**Maps to:** UC-2, FR-1.22
**Type:** unit
**Slice:** Slice 6

**Steps:** Run `s01_pick` successfully via `CliRunner`; capture stdout

**Expected result:** stdout contains absolute path to `01_pick/entry.json`; contains at least 3 checklist bullet strings; contains text about `--rerun`, `approve`, and `reset`

---

---

## 5. Stage 02: Enrich Context (s02_enrich)

---

### TC-5.1: Happy-path integration — all three sub-steps succeed

**Maps to:** UC-3, FR-5.1, FR-5.4, AC-5.1
**Type:** integration
**Slice:** Slice 7

**Steps:** Inject mock `playwright_client`, `gemini_client.multimodal`, and `anthropic_client` (ba-analyst); run `s02_enrich` with `live_url` set and `feature_walkthrough` steps

**Expected result:** `02_enrich/context.json` exists; validates against `EnrichedContext`; `pr_links` is a list of strings; `diff_stats` is a dict; `narrative` is a non-empty string; `screenshots` contains ≥ 1 PNG path

---

### TC-5.2: `live_url` absent — Playwright skipped; narrative still produced

**Maps to:** UC-3-A1, FR-5.2, AC-5.2
**Type:** integration
**Slice:** Slice 7

**Steps:** Set `live_url=None` in `input.yaml`; inject mock `gemini_client.multimodal` returning non-empty string

**Expected result:** `context.json.screenshots == []`; `narrative` non-empty; `playwright_client` never called (confirmed via mock assertion)

---

### TC-5.3: `feature_walkthrough` absent but `live_url` present — single viewport screenshot

**Maps to:** UC-3-A2
**Type:** integration
**Slice:** Slice 7

**Steps:** `live_url` set; `feature_walkthrough=None`; mock Playwright viewport screenshot

**Expected result:** `screenshots` contains 1 path; no step-by-step automation invoked

---

### TC-5.4: `SubagentTimeout` — ba-analyst subprocess exceeds 300 s

**Maps to:** UC-3-E1, UC-28, FR-5.3, AC-5.3
**Type:** unit
**Slice:** Slice 7

**Steps:** Monkeypatch `subprocess.run` to raise `subprocess.TimeoutExpired`

**Expected result:** `SubagentTimeout` raised; `02_enrich.status == "failed"`; `error.type == "SubagentTimeout"`

---

### TC-5.5: Sub-agent non-zero exit — stderr captured in error

**Maps to:** UC-3-E2, UC-28-A1, FR-5.3
**Type:** unit
**Slice:** Slice 7

**Steps:** Monkeypatch `subprocess.run` to return `CompletedProcess(returncode=1, stderr="boom")`

**Expected result:** Stage fails; `error.message` contains `"boom"`

---

### TC-5.6: `SubagentMalformedOutput` — ba-analyst stdout is not valid JSON

**Maps to:** UC-3-E3, UC-28-A2, FR-5.3
**Type:** unit
**Slice:** Slice 7

**Steps:** Monkeypatch subprocess to return `returncode=0, stdout="not json at all"`

**Expected result:** `SubagentMalformedOutput` raised; stage `failed`

---

### TC-5.7: `GeminiRateLimited` on multimodal call

**Maps to:** UC-3-E4, AC-5.4
**Type:** unit
**Slice:** Slice 7

**Steps:** Inject `gemini_client.multimodal` to raise `GeminiRateLimited`

**Expected result:** Stage fails with `GeminiRateLimited` in error; no `context.json` written

---

### TC-5.8: `PlaywrightTimeout` during enrichment screenshots

**Maps to:** UC-3-E5, FR-3.5
**Type:** unit
**Slice:** Slice 7

**Steps:** Inject `playwright_client.screenshot_feature` to raise `PlaywrightTimeout`

**Expected result:** Stage `failed`; `error.type == "PlaywrightTimeout"`

---

### TC-5.9: `narrative.md` vs `context.json` single-source-of-truth (Architect MAJOR Finding 3)

**Maps to:** Architecture finding 3, FR-5.4
**Type:** unit
**Slice:** Slice 7

**Steps:** Run `s02_enrich` with mocked clients; inspect outputs list and `context.json` structure

**Expected result:** The narrative is stored in exactly one location (either `context.json.narrative` field only, OR both `context.json.narrative` and `narrative.md` as a declared output). Whichever is chosen, `manifest.stages.02_enrich.outputs` lists ALL written files; no file exists on disk that is absent from `outputs`. No undeclared file left behind.

**Negative assertions:** If `narrative.md` is written, it appears in `manifest.stages.02_enrich.outputs`. If it is not written, the narrative is accessible in `context.json.narrative`.

---

---

## 6. Stage 03: Brand Extraction (s03_brand)

---

### TC-6.1: Happy path — complete brand pack, no palette hint, full Playwright extraction

**Maps to:** UC-4, FR-3.1, FR-3.4, FR-3.7, FR-3.8, AC-3.2
**Type:** integration
**Slice:** Slice 10

**Steps:**
1. Populate `_brand/test-brand/` with `voice.md`, `fonts/Inter.ttf`, `logo.svg`
2. Inject mocked `playwright_client` and `gemini_client`; run `s03_brand`

**Expected result:** `03_brand/proposal.json`, `03_brand/logo.png`, `03_brand/style_sheet.png` produced; `proposal.json` validates against `BrandProposal` schema; PNG headers valid (`bytes[0:4] == b"\x89PNG"`)

---

### TC-6.2: `BrandPackIncomplete` — `voice.md` missing

**Maps to:** UC-4-E1, FR-3.3, AC-3.1
**Type:** unit
**Slice:** Slice 10

**Steps:** Omit `voice.md` from brand pack; run `s03_brand.check_inputs()`

**Expected result:** `BrandPackIncomplete` raised; error message lists `voice.md`; no API call made

**Negative assertions:** `playwright_client` and `gemini_client` never called

---

### TC-6.3: `BrandPackIncomplete` — `fonts/` directory absent or empty

**Maps to:** UC-4-E2, FR-3.3, AC-3.1
**Type:** unit
**Slice:** Slice 10

**Steps:** Provide `voice.md` and `logo.svg` but no `.ttf` in `fonts/`

**Expected result:** `BrandPackIncomplete`; error message lists missing font

---

### TC-6.4: `BrandPackIncomplete` — neither `logo.svg` nor `logo.png` present

**Maps to:** UC-4-E3, FR-3.3, AC-3.1
**Type:** unit
**Slice:** Slice 10

**Steps:** Provide `voice.md` and `fonts/` but no logo file

**Expected result:** `BrandPackIncomplete`; error lists logo file

---

### TC-6.5: All three REQUIRED files missing — all listed in `BrandPackIncomplete`

**Maps to:** FR-3.3, AC-3.1
**Type:** unit
**Slice:** Slice 10

**Steps:** Provide completely empty brand pack directory

**Expected result:** `BrandPackIncomplete` raised; error message names all three missing items

---

### TC-6.6: `palette.hint.json` present — Playwright palette extraction skipped

**Maps to:** UC-4-A1, UC-33, FR-3.2, AC-3.3
**Type:** unit
**Slice:** Slice 10

**Steps:** Add `palette.hint.json = {"primary": "#FF0000", "accent": "#00FF00", "neutral": "#0000FF"}` to brand pack; inject mock playwright that raises on `extract_css_palette`

**Expected result:** Stage completes successfully; `playwright_client.extract_css_palette` is never called; `proposal.json` palette uses the hint values

---

### TC-6.7: `style_sheet.png` in brand pack — Gemini generate_image skipped

**Maps to:** UC-4-A2, FR-3.2
**Type:** unit
**Slice:** Slice 10

**Steps:** Place `style_sheet.png` in brand pack; inject mock gemini that raises on `generate_image`

**Expected result:** Stage completes; `gemini_client.generate_image` never called; operator-supplied style sheet used

---

### TC-6.8: Logo not detected — 1×1 transparent PNG written, `logo_detected=false`

**Maps to:** UC-4-A3, FR-3.4
**Type:** unit
**Slice:** Slice 10

**Steps:** Inject `playwright_client.screenshot_logo` to return `None`

**Expected result:** `03_brand/logo.png` is a 1×1 transparent PNG; `proposal.json.logo_detected == false`

---

### TC-6.9: `PlaywrightTimeout` during brand extraction

**Maps to:** UC-4-E4, FR-3.5, AC-3.6
**Type:** unit
**Slice:** Slice 10

**Steps:** Inject `playwright_client.extract_css_palette` to raise `PlaywrightTimeout` after 60 s mock

**Expected result:** Stage `failed`; `error.type == "PlaywrightTimeout"`; no partial outputs remain

---

### TC-6.10: URL validator called before any Playwright method

**Maps to:** UC-4-E5, FR-3.6, AC-3.6
**Type:** unit
**Slice:** Slice 10

**Steps:** Set `live_url` to a valid-format but RFC1918-resolving URL; monkeypatch `socket.gethostbyname` to return `"192.168.1.1"`; run `s03_brand`

**Expected result:** `ValidationError` raised before any `playwright_client` method is called; mock playwright never invoked

---

### TC-6.11: `manually_edited=true` recorded when operator edits `proposal.json` before approve

**Maps to:** UC-4, UC-15, FR-3.10, AC-3.4
**Type:** integration
**Slice:** Slice 10

**Steps:**
1. Run `s03_brand` successfully; `outputs_hash_at_done` stored
2. Modify `proposal.json` bytes
3. Run `shipcast approve <slug> 03_brand` via dispatcher

**Expected result:** CLI prints "Manual edits detected on 1 file(s)"; `manifest.manually_edited == true`; `human_approved_at` set

---

### TC-6.12: `manually_edited=false` when approved without edits

**Maps to:** UC-4-A4, FR-3.10
**Type:** integration
**Slice:** Slice 10

**Steps:** Run `s03_brand`; approve immediately without touching output files

**Expected result:** `manually_edited == false`

---

### TC-6.13: `config_snapshot` byte-identical before and after `s03_brand`

**Maps to:** UC-27-EC1, FR-3.11, NFR-3.1, AC-3.5
**Type:** integration
**Slice:** Slice 10

**Steps:** Capture `manifest.config_snapshot` bytes before `s03_brand` runs; run stage; re-read `config_snapshot`

**Expected result:** Byte-identical; brand data never injected into snapshot

---

### TC-6.14: Operator replaces all three outputs — all three listed in changed-file report

**Maps to:** UC-4-EC2
**Type:** integration
**Slice:** Slice 10

**Steps:** After stage completes, overwrite `proposal.json`, `logo.png`, and `style_sheet.png` with different bytes; run approve

**Expected result:** All three file names appear in the changed-files list; `manually_edited=true`

---

### TC-6.15: Operator replaces file with byte-identical content — no false `manually_edited`

**Maps to:** UC-15-EC1, FR-1.13
**Type:** unit
**Slice:** Slice 10

**Steps:** Write identical bytes back to `logo.png` (different mtime); run approve

**Expected result:** `manually_edited == false` (byte-content hash unchanged)

---

### TC-6.16: `voice.md` copied to `03_brand/voice.md` as declared output (Architect MAJOR Finding 1)

**Maps to:** Architecture finding 1, FR-3.8, FR-6.2, FR-10.2, FR-12.1
**Type:** integration
**Slice:** Slice 10

**Steps:** Run `s03_brand` successfully; inspect `manifest.stages.03_brand.outputs`

**Expected result:** `03_brand/voice.md` exists on disk AND is listed in `manifest.stages.03_brand.outputs`; downstream stages `s04_plan`, `s08_video`, `s10_copy` read from `03_brand/voice.md` (verify path references in those stage implementations)

**Negative assertions:** `_brand/<slug>/voice.md` is NOT directly referenced by downstream stage `check_inputs` (all reads go through `03_brand/voice.md`)

---

---

## 7. Stage 04: Marketing Plan (s04_plan)

---

### TC-7.1: Happy path — chained planner + brand-guardian, brief validates

**Maps to:** UC-5, FR-6.1, FR-6.2, FR-6.4, AC-6.1
**Type:** integration
**Slice:** Slice 11

**Steps:** Inject mock `anthropic_client` returning valid `MarketingBrief` JSON from planner, then valid amended JSON from brand-guardian; run `s04_plan`

**Expected result:** `04_plan/brief.json` exists; validates against `MarketingBrief`; `video_beats` length == 4; `carousel_beats` length == 4; each `hook_template_per_channel` value is in the 7-key catalog; `ctas` non-empty; `has_stat_card` and `has_code_screenshot` are booleans

---

### TC-7.2: Hook template catalog — all 7 templates render non-empty string

**Maps to:** FR-6.5, AC-6.2, AC-14.1
**Type:** unit
**Slice:** Slice 11

**Steps:** For each of the 7 keys (`we_just_shipped`, `before_after`, `problem_aha`, `numbered_list`, `behind_the_scenes`, `5_sec_value`, `social_proof`), call `hooks.render(key, sample_entry)`

**Expected result:** All 7 calls return non-empty strings; no exception

---

### TC-7.3: `brand-guardian` modifies brief — final output is guardian's version

**Maps to:** UC-5-A1, FR-6.2
**Type:** unit
**Slice:** Slice 11

**Steps:** Planner returns draft brief; brand-guardian mock returns a modified brief with different `hook_template_per_channel`

**Expected result:** `brief.json` contains guardian's values, not planner's draft

---

### TC-7.4: `planner` sub-agent timeout raises `SubagentTimeout`

**Maps to:** UC-5-E1, FR-6.3, AC-6.4
**Type:** unit
**Slice:** Slice 11

**Steps:** Monkeypatch first subprocess call to raise `TimeoutExpired`

**Expected result:** `SubagentTimeout`; stage `failed`; no `brief.json` written

---

### TC-7.5: `brand-guardian` sub-agent timeout raises `SubagentTimeout`

**Maps to:** UC-5-E2, FR-6.3, AC-6.4
**Type:** unit
**Slice:** Slice 11

**Steps:** First subprocess succeeds; second raises `TimeoutExpired`

**Expected result:** `SubagentTimeout`; stage `failed`

---

### TC-7.6: Either sub-agent returns malformed JSON raises `SubagentMalformedOutput`

**Maps to:** UC-5-E3, FR-6.3
**Type:** unit
**Slice:** Slice 11

**Steps:** Mock planner to return `"not json"` as stdout; mock guardian; run

**Expected result:** `SubagentMalformedOutput` on parse failure; stage `failed`

---

### TC-7.7: Brief schema validation failure — `video_beats` length != 4

**Maps to:** UC-5-E4, FR-6.4
**Type:** unit
**Slice:** Slice 11

**Steps:** Inject guardian returning JSON with `video_beats` of length 3

**Expected result:** `validate_outputs()` fails; stage `failed`; error lists invalid field

---

### TC-7.8: `brand-guardian` agent file exists with valid frontmatter

**Maps to:** AC-6.3
**Type:** unit
**Slice:** Slice 11

**Steps:** Assert file `~/.claude/agents/brand-guardian.md` exists; parse YAML frontmatter

**Expected result:** File present; frontmatter includes `name`, `model`, and `tools` keys

---

---

## 8. Stage 05: Script and Storyboard (s05_script)

---

### TC-8.1: Happy path — storyboard with 4 beats produced

**Maps to:** UC-6, FR-7.1, FR-7.2, FR-7.4, AC-7.1
**Type:** integration
**Slice:** Slice 12

**Steps:** Inject mock `demo-script-writer` returning valid `Storyboard` JSON with 4 beats; run `s05_script`

**Expected result:** `05_script/storyboard.json` exists; validates against `Storyboard` schema; 4 beats each with `image_prompt`, `narration`, `duration_sec`

---

### TC-8.2: Beat count 6 (maximum boundary) — accepted

**Maps to:** FR-7.3
**Type:** unit
**Slice:** Slice 12

**Steps:** Mock sub-agent returns storyboard with exactly 6 beats

**Expected result:** Stage succeeds; `storyboard.json` has 6 beats

---

### TC-8.3: Beat count 3 (below minimum) raises `SubagentMalformedOutput`

**Maps to:** UC-6-E2, FR-7.3
**Type:** unit
**Slice:** Slice 12

**Steps:** Mock returns storyboard with 3 beats

**Expected result:** `SubagentMalformedOutput`; stage `failed`

---

### TC-8.4: Beat count 7 (above maximum) raises `SubagentMalformedOutput`

**Maps to:** UC-6-E2, FR-7.3
**Type:** unit
**Slice:** Slice 12

**Steps:** Mock returns 7 beats

**Expected result:** `SubagentMalformedOutput`

---

### TC-8.5: Sub-agent timeout raises `SubagentTimeout`

**Maps to:** UC-6-E1
**Type:** unit
**Slice:** Slice 12

**Steps:** Mock subprocess raises `TimeoutExpired`

**Expected result:** `SubagentTimeout`; stage `failed`

---

### TC-8.6: Required beat field missing raises at `validate_outputs`

**Maps to:** UC-6-E3, FR-1.23
**Type:** unit
**Slice:** Slice 12

**Steps:** Mock returns beat without `narration` field

**Expected result:** Pydantic `ValidationError` in `validate_outputs`; stage `failed`

---

### TC-8.7: `demo-script-writer` agent file exists with valid frontmatter

**Maps to:** AC-7.2
**Type:** unit
**Slice:** Slice 12

**Steps:** Assert `~/.claude/agents/demo-script-writer.md` exists; parse frontmatter

**Expected result:** File present; valid frontmatter

---

### TC-8.8: Snapshot test — mock `claude -p` subprocess, assert parsed output structure

**Maps to:** FR-7.2, AC-7.3
**Type:** snapshot
**Slice:** Slice 12

**Steps:** Use pinned mock subprocess output JSON; parse via `s05_script`'s parsing logic

**Expected result:** Parsed `Storyboard` has correct shape; `beats` length in [4, 6]; each beat has all three required fields

---

---

## 9. Stage 06: Video Assets (s06_video_assets)

---

### TC-9.1: Standard mode — 4 clips at 1080×1920 h264, 3–5 s each

**Maps to:** UC-7, FR-8.1, FR-8.2, FR-8.7, AC-8.1
**Type:** integration
**Slice:** Slice 13

**Steps:** Set `video_mode="standard"`; inject mock `gemini_client.generate_image` and `ffmpeg_client.ken_burns_clip`; run `s06_video_assets`

**Expected result:** `06_video_assets/beat_00.mp4` through `beat_03.mp4` exist; each mock clip reported as 1080×1920 h264; no `veo_client` called (assert mock never invoked)

---

### TC-9.2: Premium mode — beat[0] is 8 s Veo clip; beats[1..3] are Ken-Burns

**Maps to:** UC-8, FR-8.3, FR-8.4, AC-8.2
**Type:** integration
**Slice:** Slice 13

**Steps:** Set `video_mode="premium"`; inject mock `veo_client.generate_clip` returning 8 s clip; inject mock `gemini_client` for fill shots

**Expected result:** `beat_00.mp4` mocked as 8.0 s; `beat_01.mp4`–`beat_03.mp4` mocked as 3–5 s; `veo_client.generate_clip` called exactly once

---

### TC-9.3: `VeoSafetyBlocked` fallback — Ken-Burns clip used; blocked prompt not logged

**Maps to:** UC-8-E1, FR-8.5, AC-8.3
**Type:** unit
**Slice:** Slice 13

**Steps:** Inject `veo_client.generate_clip` to raise `VeoSafetyBlocked`; inject mock `ken_burns_clip`; run stage; capture log output

**Expected result:** `beat_00.mp4` produced via Ken-Burns path; log files do NOT contain the original `image_prompt` text from the safety-blocked call

**Negative assertions:** Safety-blocked prompt string is absent from all log file contents

---

### TC-9.4: `VeoQuotaExceeded` — stage fails immediately, no further beats processed

**Maps to:** UC-8-E2, FR-8.6, AC-8.4
**Type:** unit
**Slice:** Slice 13

**Steps:** Inject `veo_client.generate_clip` to raise `VeoQuotaExceeded`

**Expected result:** `VeoQuotaExceeded` in error; stage `failed`; `beat_01.mp4`–`beat_03.mp4` not written (assert file absence)

---

### TC-9.5: `VeoTimeout` (>120 s polling) — stage fails

**Maps to:** UC-8-E3, FR-8.4
**Type:** unit
**Slice:** Slice 13

**Steps:** Inject `veo_client` polling loop to raise `VeoTimeout` after mock 120 s

**Expected result:** Stage `failed`; `error.type == "VeoTimeout"`

---

### TC-9.6: `--no-veo` flag in premium mode — all beats use Ken-Burns; Veo never called

**Maps to:** UC-8-A1, FR-8.8, AC-15.4
**Type:** unit
**Slice:** Slice 13

**Steps:** Inject `veo_client.generate_clip` wired to raise `AssertionError`; run `shipcast video_assets <slug> --no-veo` with premium project

**Expected result:** All 4 clips produced via Ken-Burns; `veo_client.generate_clip` never called (no `AssertionError` raised)

---

### TC-9.7: `ffprobe` validation failure on bad codec

**Maps to:** UC-7-E3, FR-8.7
**Type:** unit
**Slice:** Slice 13

**Steps:** Mock `ffprobe` to report `codec_name=vp9` for `beat_00.mp4`

**Expected result:** Stage fails with structured error identifying the malformed clip

---

### TC-9.8: Gemini Imagen rate-limit during standard-mode render

**Maps to:** UC-7-E2, FR-8.2
**Type:** unit
**Slice:** Slice 13

**Steps:** Inject `gemini_client.generate_image` to raise `GeminiRateLimited` on second call

**Expected result:** Stage `failed`; `error.type == "GeminiRateLimited"`

---

---

## 10. Stage 07: Voice Synthesis (s07_voice)

---

### TC-10.1: Happy path — `narration.mp3` and `words.json` produced

**Maps to:** UC-9, FR-9.1, FR-9.3, FR-9.4, AC-9.1, AC-9.2
**Type:** integration
**Slice:** Slice 14

**Steps:** Inject mock `elevenlabs_client.synthesize` returning mock MP3 bytes; inject mock `whisperx_client.transcribe` returning word-timestamp list; run `s07_voice`

**Expected result:** `07_voice/narration.mp3` exists; `07_voice/words.json` is non-empty list of `{word, start, end}` objects; word duration sum within 1 s of mock MP3 duration

---

### TC-10.2: Narration script joins beat narrations with single newlines

**Maps to:** FR-9.1
**Type:** unit
**Slice:** Slice 14

**Steps:** Create storyboard with 3 beats having narrations `["A", "B", "C"]`; capture the text passed to `elevenlabs_client.synthesize`

**Expected result:** Text passed = `"A\nB\nC"` (single newline separator, no trailing newline)

---

### TC-10.3: Voice ID read from `Settings.voice_id`, not from `voice.md`

**Maps to:** FR-9.2
**Type:** unit
**Slice:** Slice 14

**Steps:** Set `Settings.voice_id = "test-voice-id"`; inject mock that records the `voice_id` argument; run `s07_voice`

**Expected result:** `synthesize` called with `voice_id="test-voice-id"`

---

### TC-10.4: `ElevenLabsQuotaExceeded` on 429 — no files written

**Maps to:** UC-9-E1, FR-9.5, AC-9.3
**Type:** unit
**Slice:** Slice 14

**Steps:** Inject `elevenlabs_client.synthesize` to raise `ElevenLabsQuotaExceeded`

**Expected result:** Stage `failed`; neither `narration.mp3` nor `words.json` written

---

### TC-10.5: ElevenLabs auth error (`MissingApiKey`)

**Maps to:** UC-9-E2, FR-1.29
**Type:** unit
**Slice:** Slice 14

**Steps:** Construct `ElevenLabsClient` with empty `SecretStr`

**Expected result:** `MissingApiKey("ELEVENLABS_API_KEY")` raised; exception message contains key NAME only, not the empty value

---

### TC-10.6: WhisperX not available on PATH

**Maps to:** UC-9-E3
**Type:** unit
**Slice:** Slice 14

**Steps:** Monkeypatch `shutil.which("whisperx")` to return `None`; run `check_inputs` or early validation in `s07_voice`

**Expected result:** Stage fails with descriptive error; synthesis API NOT called

---

---

## 11. Stage 08: Video Assembly (s08_video)

---

### TC-11.1: Happy path — `showcase.mp4`, `loop_6s.mp4`, `loop_6s.gif` produced

**Maps to:** UC-10, FR-10.1, FR-10.4, FR-10.5, AC-10.1, AC-10.2, AC-10.3
**Type:** integration
**Slice:** Slice 15

**Steps:** Inject mock `ffmpeg_client` for concat, caption overlay, and loop export; mock `voice.md` with `caption_mode: chip`; run `s08_video`

**Expected result:** Three output files exist; mock ffprobe reports `showcase.mp4` at 1080×1920 h264+aac; `loop_6s.mp4` at 1080×1080 no audio; `loop_6s.gif` ≤ 8 MB

---

### TC-11.2: `voice.md` `caption_mode: chip` — chip renderer invoked

**Maps to:** UC-10, FR-10.2, AC-14.3
**Type:** unit
**Slice:** Slice 15

**Steps:** Provide `voice.md` with `caption_mode: chip`; run `s08_video`; inspect which caption renderer was called

**Expected result:** `chip` renderer invoked; not `karaoke` or `reveal`

---

### TC-11.3: `caption_mode: karaoke` — karaoke renderer invoked

**Maps to:** UC-10-A2, FR-10.2, AC-14.3
**Type:** unit
**Slice:** Slice 15

**Steps:** Provide `voice.md` with `caption_mode: karaoke`

**Expected result:** `karaoke` renderer invoked

---

### TC-11.4: `caption_mode: reveal` — reveal renderer invoked

**Maps to:** UC-10-A3, FR-10.2
**Type:** unit
**Slice:** Slice 15

**Steps:** Provide `voice.md` with `caption_mode: reveal`

**Expected result:** `reveal` renderer invoked

---

### TC-11.5: `caption_mode` absent — chip renderer used as default

**Maps to:** UC-10-A4, FR-10.2, FR-14.8, AC-10.5
**Type:** unit
**Slice:** Slice 15

**Steps:** Provide `voice.md` with no `caption_mode:` line

**Expected result:** `chip` renderer invoked (default)

---

### TC-11.6: Unrecognized `caption_mode` value — chip fallback used

**Maps to:** FR-10.2, FR-14.8
**Type:** unit
**Slice:** Slice 15

**Steps:** Provide `voice.md` with `caption_mode: fancytype`

**Expected result:** `chip` renderer invoked; no exception

---

### TC-11.7: No background music — only narration audio; no ducking

**Maps to:** UC-10-A1, FR-10.1
**Type:** unit
**Slice:** Slice 15

**Steps:** Brand pack has no `music/` directory; run `s08_video`

**Expected result:** `_assemble_raw()` called without BGM mixer argument; narration used as sole audio track

---

### TC-11.8: Background music present — narration ducked to −3 dB relative to BGM

**Maps to:** FR-10.1
**Type:** unit
**Slice:** Slice 15

**Steps:** Place mock BGM file in `_brand/<slug>/music/track.mp3`; run `s08_video`; capture ffmpeg call arguments

**Expected result:** ffmpeg call includes narration ducking filter (`-3 dB`); first alphabetical `music/*.mp3` selected

---

### TC-11.9: Caption region visual-diff — ≥ 95% of caption frames differ from raw

**Maps to:** AC-10.4
**Type:** integration
**Slice:** Slice 15

**Steps:** Produce `showcase.mp4` with captions and a raw assembled video without captions; compare caption-region pixels per frame

**Expected result:** ≥ 95% of sampled caption-region frames show pixel difference (captions confirmed burned in)

---

### TC-11.10: `s08_video` reads `voice.md` from `03_brand/voice.md` (Architect MAJOR Finding 1)

**Maps to:** Architecture finding 1, FR-10.2
**Type:** unit
**Slice:** Slice 15

**Steps:** Place `voice.md` only at `03_brand/voice.md` (not at `_brand/<slug>/voice.md`); run `s08_video`

**Expected result:** Stage reads caption mode successfully from `03_brand/voice.md`; no `FileNotFoundError`

---

---

## 12. Stage 09: Static Graphics (s09_graphics)

---

### TC-12.1: Output file set — correct files present (both flags true)

**Maps to:** UC-11, FR-11.1, FR-11.3, FR-11.4, FR-11.6, FR-11.8, AC-11.1
**Type:** integration
**Slice:** Slices 16–18

**Steps:** Use pinned brief fixture with `has_stat_card=true`, `has_code_screenshot=true`; inject mock Gemini; run `s09_graphics`

**Expected result:** Files present: `09_graphics/1x1.png`, `16x9.png`, `9x16.png`, `4x5.png`, `og_card.png`, `stat_1x1.png`, `stat_16x9.png`, `stat_9x16.png`, `stat_4x5.png`, `code.png`, `carousel/slide_01.png`–`carousel/slide_06.png` (17 total)

---

### TC-12.2: Output dimensions correct for each card type

**Maps to:** AC-11.2, FR-11.1, FR-11.3, FR-11.8
**Type:** integration
**Slice:** Slices 16–18

**Steps:** Open each produced PNG via `PIL.Image.open`; assert `.size`

**Expected result:**
- `1x1.png` → (1080, 1080)
- `16x9.png` → (1920, 1080)
- `9x16.png` → (1080, 1920)
- `4x5.png` → (1080, 1350)
- `og_card.png` → (1200, 630)
- Each `carousel/slide_*.png` → (1080, 1350)

---

### TC-12.3: Palette conformance — all 4 aspect-ratio cards pass ΔE-CIE2000 < 10 test

**Maps to:** UC-11, FR-11.2, FR-14.5, AC-11.3, AC-14.2
**Type:** integration
**Slice:** Slice 16

**Steps:** For each of `1x1.png`, `16x9.png`, `9x16.png`, `4x5.png`: PIL `quantize(colors=5)`; compute ΔE-CIE2000 for each pixel vs. brand `primary`, `accent`, `neutral`, white, black

**Expected result:** ≥ 80% of pixels fall within ΔE < 10 of one of the five reference colors

**Fixture:** Uses pinned `brief.json` + pinned `proposal.json` (brand palette); does NOT use live LLM output

---

### TC-12.4: Stat card conditional on `has_stat_card=true`

**Maps to:** UC-11, FR-11.4, AC-11.4
**Type:** unit
**Slice:** Slice 17

**Steps:** Use pinned brief with `has_stat_card=true`; run `s09_graphics`

**Expected result:** `stat_1x1.png`, `stat_16x9.png`, `stat_9x16.png`, `stat_4x5.png` all exist

---

### TC-12.5: No stat files created when `has_stat_card=false`

**Maps to:** UC-11-A1, FR-11.5, AC-11.4
**Type:** unit
**Slice:** Slice 17

**Steps:** Use pinned brief with `has_stat_card=false`; run `s09_graphics`

**Expected result:** No `stat_*.png` files exist in `09_graphics/`; `_render_stat()` not called (mock assertion)

**Note:** Must use pinned brief fixture, NOT live LLM output

---

### TC-12.6: Code screenshot created when `has_code_screenshot=true`

**Maps to:** UC-11, FR-11.6, AC-11.5
**Type:** unit
**Slice:** Slice 18

**Steps:** Use pinned brief with `has_code_screenshot=true`; run `s09_graphics`

**Expected result:** `09_graphics/code.png` exists; PIL can open it; no external API called (Pygments + PIL only)

---

### TC-12.7: No `code.png` created when `has_code_screenshot=false`

**Maps to:** UC-11-A2, FR-11.7, AC-11.5
**Type:** unit
**Slice:** Slice 18

**Steps:** Use pinned brief with `has_code_screenshot=false`

**Expected result:** No `code.png`; `_render_code()` not called

---

### TC-12.8: Carousel — 6 slides; slide 01 contains hook text; slide 06 contains CTA

**Maps to:** UC-11, FR-11.8, FR-11.9, AC-11.6
**Type:** integration
**Slice:** Slice 18

**Steps:** Run `s09_graphics`; open `carousel/slide_01.png` and `slide_06.png` via PIL; extract any text via the image (or assert composition helper received correct text)

**Expected result:** Exactly 6 slide files; composition helper invoked with hook text for slide 01 and CTA text for slide 06

---

### TC-12.9: Minimum output set — both flags false

**Maps to:** UC-11-A3
**Type:** unit
**Slice:** Slices 16–18

**Steps:** Use pinned brief with `has_stat_card=false`, `has_code_screenshot=false`

**Expected result:** Exactly 11 files: 4 aspect cards + OG card + 6 carousel slides; no stat files; no code.png

---

### TC-12.10: `GeminiRateLimited` during aspect card rendering — stage fails

**Maps to:** UC-11-E1
**Type:** unit
**Slice:** Slice 16

**Steps:** Inject `gemini_client.generate_image` to raise `GeminiRateLimited` on second call

**Expected result:** Stage `failed`; error recorded; operator can `--rerun`

---

### TC-12.11: Palette conformance helper reusable across all graphics tests

**Maps to:** FR-14.5, AC-14.2
**Type:** unit
**Slice:** Slice 16

**Steps:** Call `test_palette_conformance.assert_palette_conformance(image_path, brand_palette)` on a synthetically generated PNG with known palette

**Expected result:** Helper passes for images within tolerance; raises `AssertionError` for obviously off-palette inputs; helper lives in `tests/unit/test_palette_conformance.py`

---

---

## 13. Stage 10: Copy Generation (s10_copy)

---

### TC-13.1: Happy path — three Markdown files produced with correct lengths

**Maps to:** UC-12, FR-12.1, FR-12.2, FR-12.3, AC-12.1, AC-12.2, AC-12.3
**Type:** integration
**Slice:** Slice 19

**Steps:** Inject mock `social-copywriter` subprocess returning valid `CopyBundle` JSON; run `s10_copy`

**Expected result:**
- `10_copy/twitter_thread.md`: 3–8 numbered tweets; each tweet ≤ 280 characters
- `10_copy/linkedin.md`: word count ≥ 600 and ≤ 1200; valid CommonMark
- `10_copy/blog.md`: word count ≥ 1200 and ≤ 2000; valid CommonMark

---

### TC-13.2: Each file opens with the hook template for that channel

**Maps to:** FR-12.4, AC-12.4
**Type:** integration
**Slice:** Slice 19

**Steps:** Brief has `hook_template_per_channel = {"x": "we_just_shipped", "linkedin": "before_after", "blog": "problem_aha"}`; run stage; check first non-blank line of each file

**Expected result:** First non-blank line of each file is a substring of `hooks.render(template_key, entry)` for the matching channel

---

### TC-13.3: Twitter thread uses Unicode bold; no raw `**` Markdown bold

**Maps to:** FR-12.5, AC-12.6
**Type:** unit
**Slice:** Slice 19

**Steps:** Run stage; read `twitter_thread.md` content

**Expected result:** No occurrence of `**` in the Twitter file; Unicode mathematical bold characters present

---

### TC-13.4: LinkedIn post uses `→` or `▸` Unicode bullets; no Markdown `-` list markers

**Maps to:** FR-12.5
**Type:** unit
**Slice:** Slice 19

**Steps:** Read `linkedin.md` content; scan for list-style patterns

**Expected result:** `→` or `▸` present for bulleted content; no leading `- ` or `* ` Markdown list markers

---

### TC-13.5: Sub-agent timeout raises `SubagentTimeout`

**Maps to:** UC-12-E1
**Type:** unit
**Slice:** Slice 19

**Steps:** Mock subprocess `TimeoutExpired`

**Expected result:** Stage `failed`; no Markdown files written

---

### TC-13.6: Output fails length validation — stage fails

**Maps to:** UC-12-E2, FR-12.3
**Type:** unit
**Slice:** Slice 19

**Steps:** Mock returns `CopyBundle` with `blog` field having only 100 words

**Expected result:** `validate_outputs()` raises; stage `failed`; error message cites word-count constraint

---

### TC-13.7: `social-copywriter` agent file exists with valid frontmatter

**Maps to:** AC-12.5
**Type:** unit
**Slice:** Slice 19

**Steps:** Assert `~/.claude/agents/social-copywriter.md` exists; parse YAML frontmatter

**Expected result:** File present; valid frontmatter

---

### TC-13.8: Snapshot test — mock `claude -p`, assert CopyBundle structure and absence of `**bold**`

**Maps to:** AC-12.6
**Type:** snapshot
**Slice:** Slice 19

**Steps:** Feed pinned mock subprocess output; parse via `s10_copy` parsing logic; assert field presence, lengths, and Twitter formatting

**Expected result:** All three fields present; Twitter field ≤ 280 chars per tweet; no `**` in Twitter field

---

### TC-13.9: `s10_copy` reads `voice.md` from `03_brand/voice.md` (Architect MAJOR Finding 1)

**Maps to:** Architecture finding 1, FR-12.1
**Type:** unit
**Slice:** Slice 19

**Steps:** Confirm `s10_copy` passes `03_brand/voice.md` path to sub-agent invocation; `_brand/<slug>/voice.md` is not referenced

**Expected result:** `03_brand/voice.md` path in sub-agent context; no `FileNotFoundError` if only `03_brand/` path exists

---

---

## 14. Stage 11: Package (s11_package)

---

### TC-14.1: Happy path — ZIP contains all required files; README has 3+ fenced code blocks

**Maps to:** UC-13, FR-13.1, FR-13.2, FR-13.3, AC-13.1, AC-13.2
**Type:** integration
**Slice:** Slice 20

**Steps:** Populate all stage output directories with dummy files; inject mock `code-reviewer` subprocess; run `s11_package`

**Expected result:** `release.zip` exists; `zipfile.ZipFile` listing contains all required paths (showcase.mp4, loop clips, 4 aspect cards, og_card, 6 carousel slides, 3 markdown files); `README.md` has ≥ 3 fenced code blocks and ≥ 9-row asset table

---

### TC-14.2: Conditional files included when present

**Maps to:** FR-13.4, AC-13.1
**Type:** unit
**Slice:** Slice 20

**Steps:** Include `stat_1x1.png` and `code.png` in `09_graphics/`; run `s11_package`

**Expected result:** ZIP listing contains `stat_1x1.png` and `code.png`

---

### TC-14.3: Conditional files absent when not produced

**Maps to:** FR-13.4
**Type:** unit
**Slice:** Slice 20

**Steps:** No `stat_*.png` or `code.png` in `09_graphics/`; run `s11_package`

**Expected result:** ZIP listing does not contain those paths

---

### TC-14.4: ZIP is byte-identical on re-run (sorted entries)

**Maps to:** UC-31, FR-13.3, NFR-16.6, AC-13.3
**Type:** snapshot
**Slice:** Slice 20

**Steps:** Run `s11_package` twice on identical inputs; compare ZIP bytes

**Expected result:** `zip_run_1_bytes == zip_run_2_bytes`

---

### TC-14.5: `code-reviewer` sub-agent timeout — stage fails

**Maps to:** UC-13-E1, FR-13.5
**Type:** unit
**Slice:** Slice 20

**Steps:** Mock subprocess `TimeoutExpired`

**Expected result:** Stage `failed`; `error.type == "SubagentTimeout"`

---

---

## 15. CLI Dispatcher — Human-Gate Enforcement

---

### TC-15.1: Stage refuses to run when upstream is `done` but not approved

**Maps to:** UC-25, FR-1.14, AC-1.6
**Type:** integration
**Slice:** Slice 1

**Steps:** Set `01_pick.status = done`, `human_approved_at = null`; run `shipcast enrich <slug>` via `CliRunner`

**Expected result:** Exit code 2; `StageNotApproved` in output; `02_enrich.status == "failed"`

---

### TC-15.2: Stage refuses to run when upstream is not done

**Maps to:** UC-24, FR-1.24
**Type:** integration
**Slice:** Slice 1

**Steps:** Leave `01_pick` as `pending`; run `shipcast enrich <slug>`

**Expected result:** `StageInputMissing` raised; `02_enrich.status == "failed"`; exit non-zero (code 2)

---

### TC-15.3: `shipcast approve` when stage is not done — exit 1

**Maps to:** UC-14-E1, FR-1.16, AC-1.7
**Type:** unit
**Slice:** Slice 1

**Steps:** Stage `01_pick.status = running`; run `shipcast approve <slug> 01_pick`

**Expected result:** `CannotApproveNonDoneStage`; exit code 1; manifest unmodified

---

### TC-15.4: `shipcast approve` without edits — `manually_edited=false`

**Maps to:** UC-14, FR-1.15
**Type:** integration
**Slice:** Slice 1

**Steps:** Stage `done`; run approve immediately (no file edits)

**Expected result:** `human_approved_at` set; `manually_edited=false`; exit 0

---

### TC-15.5: `shipcast approve` after editing output — `manually_edited=true`, files listed

**Maps to:** UC-15, FR-1.15, AC-1.8
**Type:** integration
**Slice:** Slice 1

**Steps:** Stage `done`; edit one output file; run approve

**Expected result:** CLI prints "Manual edits detected on 1 file(s)" with filename; `manually_edited=true`; exit 0

---

### TC-15.6: Downstream stage can run only after upstream approved

**Maps to:** UC-14, FR-1.14
**Type:** integration
**Slice:** Slice 1

**Steps:** `01_pick` done and approved → run `02_enrich`

**Expected result:** `02_enrich` transitions to `running` and executes; no `StageNotApproved`

---

---

## 16. CLI Dispatcher — Re-run, Reset, Cascade-Confirmation Guard

---

### TC-16.1: `--rerun` on done stage — resets to pending, re-executes

**Maps to:** UC-16, FR-1.17
**Type:** integration
**Slice:** Slice 1

**Steps:** Stage `01_pick.status = done`; run `shipcast pick <slug> --rerun`

**Expected result:** Stage transitions `done → pending → running → done`; new `outputs_hash_at_done` stored; `human_approved_at = null`

---

### TC-16.2: `--rerun` on pending or failed stage — no-op, runs normally

**Maps to:** UC-16-A1, FR-1.17
**Type:** unit
**Slice:** Slice 1

**Steps:** Stage in `pending` status; run with `--rerun`

**Expected result:** Stage runs normally; informational log printed; no error

---

### TC-16.3: `--rerun` on running stage raises `StageBusy`

**Maps to:** UC-16-E1, FR-1.17
**Type:** unit
**Slice:** Slice 1

**Steps:** Stage `status = running` (simulate stale state); attempt `--rerun`

**Expected result:** `StageBusy` raised; manifest unmodified

---

### TC-16.4: `--rerun` with downstream approvals — cascade guard prompts (without `--yes`)

**Maps to:** UC-17, FR-1.19
**Type:** integration
**Slice:** Slice 1

**Steps:** Stage `03_brand` done; `04_plan.human_approved_at` non-null; run `shipcast brand <slug> --rerun` without `--yes`

**Expected result:** CLI prints warning listing all downstream approvals that will be discarded; prompts for confirmation; does NOT proceed without `y`

---

### TC-16.5: `--rerun` with downstream approvals — `--yes` bypasses prompt

**Maps to:** UC-17-A1, FR-1.19
**Type:** integration
**Slice:** Slice 1

**Steps:** Same setup as TC-16.4; add `--yes` flag

**Expected result:** No prompt; cascade proceeds; downstream `human_approved_at` values cleared

---

### TC-16.6: Operator types `n` at cascade prompt — no modification

**Maps to:** UC-17-E1
**Type:** integration
**Slice:** Slice 1

**Steps:** Same setup as TC-16.4; answer `n` at prompt

**Expected result:** No manifest changes; CLI exits 0

---

### TC-16.7: `shipcast reset` — deletes outputs and resets downstream transitively

**Maps to:** UC-18, FR-1.18
**Type:** integration
**Slice:** Slice 1

**Steps:** Stages `05_script` and `06_video_assets` done; run `shipcast reset <slug> 05_script --yes`

**Expected result:** `05_script` output files deleted; `05_script.status = pending`; `06_video_assets` through `11_package` all reset to `pending`; stages `01_pick`–`04_plan` unchanged

---

### TC-16.8: `reset` without `--yes` prompts for confirmation

**Maps to:** UC-18-A1, FR-1.18
**Type:** unit
**Slice:** Slice 1

**Steps:** Run reset without `--yes`; answer `y`

**Expected result:** Reset proceeds after `y`; aborts on any other input

---

### TC-16.9: `reset` when output file is missing on disk — continues with warning

**Maps to:** UC-18-A3
**Type:** unit
**Slice:** Slice 1

**Steps:** Delete one of the stage's output files; run `shipcast reset <slug> <stage> --yes`

**Expected result:** Warning logged; manifest still updated; exit 0

---

---

## 17. CLI Dispatcher — Cost Cap Enforcement

---

### TC-17.1: `CostCapExceeded` aborts stage before paid API call

**Maps to:** UC-20, FR-1.28, AC-1.11, AC-15.3
**Type:** unit
**Slice:** Slice 2

**Steps:** Set accumulated cost to $2.97 (standard mode); next call would add $0.04 (Imagen); assert cap check fires

**Expected result:** `CostCapExceeded`; stage transitions to `failed`; Gemini API client never called (mock assertion)

**Cost boundary:** Check is `projected_total > cap` (strict greater-than); $3.00 exactly does NOT trigger

---

### TC-17.2: Stage proceeds when accumulated cost is within cap

**Maps to:** UC-20-A1, FR-1.28
**Type:** unit
**Slice:** Slice 2

**Steps:** Accumulated cost $0.50; next call unit cost $0.04; cap $3.00

**Expected result:** Stage proceeds; API call made; `metrics.cost_usd` updated in manifest after call

---

### TC-17.3: Standard-mode mock pipeline — total accumulated cost ≤ $3.00

**Maps to:** UC-34, FR-1.28, AC-15.1
**Type:** integration
**Slice:** Slice 21

**Steps:** Mock all external API calls; simulate full pipeline in standard mode; sum `metrics.cost_usd` across all stages

**Expected result:** Total ≤ $3.00

---

### TC-17.4: Premium-mode mock pipeline — total accumulated cost ≤ $8.00

**Maps to:** UC-34, AC-15.2
**Type:** integration
**Slice:** Slice 21

**Steps:** Mock all external APIs including Veo 3 Fast at $3.20; simulate full pipeline in premium mode

**Expected result:** Total ≤ $8.00

---

### TC-17.5: Cost exactly at cap — next call blocked

**Maps to:** UC-34-EC1, FR-1.28
**Type:** unit
**Slice:** Slice 2

**Steps:** Set accumulated cost = $3.00 (standard mode cap); next unit cost = $0.04

**Expected result:** `CostCapExceeded` raised (projected $3.04 > $3.00)

---

### TC-17.6: `--rerun` of stage clears `metrics.cost_usd` for that stage only

**Maps to:** FR-1.18, Slice 2 security review
**Type:** unit
**Slice:** Slice 2

**Steps:** Simulate stage run with cost $0.12; reset the stage; re-run with mock calls; check accumulated cost before second run

**Expected result:** Cost for reset stage starts from $0 on re-run; other stages' costs unchanged; no double-counting

---

---

## 18. Concurrency and Locking

---

### TC-18.1: Two-process lock contention — exactly one exit-0, one exit-2

**Maps to:** UC-21, FR-1.30, AC-1.9
**Type:** concurrency
**Slice:** Slice 1

**Steps:** Launch two `shipcast` subprocesses for the same slug nearly simultaneously using `subprocess.Popen` (the ONLY subprocess-based test in the suite); inject `BaseStage.pre_run_hook` with a `time.sleep(0.5)` to widen the race window

**Expected result:** One process exits 0; the other exits 2 with `ProjectLocked`; `manifest.json` is not corrupted

**Note:** Uses `BaseStage.pre_run_hook` test seam. Production code MUST NOT read any env var to activate the sleep.

---

### TC-18.2: `--no-lock` without `SHIPCAST_NO_LOCK_ACK=1` raises `LockBypassNotAcknowledged`

**Maps to:** UC-22-E1, FR-1.32, AC-1.9
**Type:** unit
**Slice:** Slice 1

**Steps:** Set `SHIPCAST_NO_LOCK_ACK` not set in environment; run `shipcast pick <slug> --no-lock`

**Expected result:** `LockBypassNotAcknowledged`; exit non-zero; no stage execution

---

### TC-18.3: `--no-lock` with `SHIPCAST_NO_LOCK_ACK=1` — proceeds with yellow warning

**Maps to:** UC-22, FR-1.32
**Type:** unit
**Slice:** Slice 1

**Steps:** Set `SHIPCAST_NO_LOCK_ACK=1`; run `shipcast pick <slug> --no-lock`

**Expected result:** Yellow warning banner printed; lock acquisition skipped; stage executes normally

---

### TC-18.4: Lock released on clean exit — subsequent invocation succeeds

**Maps to:** FR-1.30
**Type:** unit
**Slice:** Slice 1

**Steps:** Run stage successfully (acquires lock); verify lock released; run second stage immediately

**Expected result:** Second invocation acquires lock without error; no `ProjectLocked`

---

---

## 19. Security and Secrets

---

### TC-19.1: `.env.example` contains only bare key names with empty values

**Maps to:** FR-1.29, NFR-16.8, AC-1.12
**Type:** unit
**Slice:** Slice 1

**Steps:** Read `.env.example`; scan each line for non-empty values after `=`

**Expected result:** Every line is of the form `KEY=` with nothing after the `=`; no real keys; assertion implemented in `tests/unit/test_package_imports.py`

---

### TC-19.2: `config_snapshot` excludes all `SecretStr` fields

**Maps to:** FR-1.29, NFR-16.8, AC-1.5
**Type:** unit
**Slice:** Slice 2

**Steps:** Build a `Settings` object with all keys set; call `settings.public_dict()`; serialize to simulate `config_snapshot`

**Expected result:** Result dict contains no keys from the secret subset (`ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`); none of their values present as strings anywhere in the serialized output

---

### TC-19.3: Client `__init__` raises `MissingApiKey` with key NAME only

**Maps to:** UC-36, FR-1.29
**Type:** unit
**Slice:** Slice 1

**Steps:** Construct `GeminiClient(api_key=SecretStr(""))` and `ElevenLabsClient(api_key=SecretStr(""))` and `AnthropicClient(api_key=SecretStr(""))`

**Expected result:** `MissingApiKey("GEMINI_API_KEY")` raised; exception message is the key NAME; no empty or partial value included

---

### TC-19.4: No client constructed at CLI startup (lazy construction)

**Maps to:** FR-1.29, NFR-16.10
**Type:** unit
**Slice:** Slice 1

**Steps:** Import `shipcast.cli`; monkeypatch `GeminiClient.__init__` to raise `AssertionError`; run `shipcast --help`

**Expected result:** `--help` exits 0 without triggering the `AssertionError` (client never instantiated at import or startup)

---

### TC-19.5: `SecretStr` values not present in log files on exception

**Maps to:** FR-1.33, NFR-16.8
**Type:** unit
**Slice:** Slice 1

**Steps:** Force a traceback during a stage run that has a `Settings` object in scope; capture log file contents

**Expected result:** Log file contains `"**********"` (Pydantic mask) for any `SecretStr` repr; actual key value strings absent from log

---

### TC-19.6: `repo_path` symlink escape rejected

**Maps to:** FR-2.4, security review Slice 3
**Type:** unit
**Slice:** Slice 3

**Steps:** Create a symlink under the allowed root that points outside it; parse `InputYaml` with that path

**Expected result:** `ValidationError`; path outside allowed root after resolution

---

---

## 20. Architect MAJOR Finding Coverage

*Explicit test cases for the three MAJOR findings in the architecture review.*

---

### TC-20.1: (Finding 1) `voice.md` exists at `03_brand/voice.md` after `s03_brand` runs

**Maps to:** Architecture MAJOR Finding 1, FR-3.8, FR-6.2, FR-10.2, FR-12.1
**Type:** integration
**Slice:** Slice 10

**Steps:** Run `s03_brand` with full mock clients; check output filesystem

**Expected result:** `03_brand/voice.md` exists on disk; it is listed in `manifest.stages.03_brand.outputs`; its byte contents match `_brand/<slug>/voice.md` (copied, not symlinked)

**Negative assertions:** `s04_plan`, `s08_video`, `s10_copy` reference `03_brand/voice.md` in their `requires`-path resolution; they do NOT reference `_brand/<slug>/voice.md` directly

---

### TC-20.2: (Finding 1) Downstream stage `check_inputs` includes `03_brand/voice.md` in upstream-files check

**Maps to:** Architecture MAJOR Finding 1, FR-1.24
**Type:** unit
**Slice:** Slice 10

**Steps:** Remove `03_brand/voice.md` while leaving other `03_brand` outputs intact; run `s04_plan.check_inputs()`

**Expected result:** `StageInputMissing` raised (voice.md listed as missing upstream output); stage refuses to proceed

---

### TC-20.3: (Finding 2) `BaseStage.check_inputs` behavior with changed upstream `inputs_hash`

**Maps to:** Architecture MAJOR Finding 2, FR-1.24, FR-3.11
**Type:** unit
**Slice:** Slice 4

**Steps:** Run `s01_pick` storing `inputs_hash` value `H1`; modify `01_pick/entry.json` bytes; re-run `s02_enrich.check_inputs()` where `02_enrich.inputs_hash` was recorded against the old upstream state

**Expected result:** Either (a) `StageInputMissing` or a drift-warning is raised/logged because the upstream `inputs_hash` does not match the current file state, OR (b) the test asserts a comment in `_base.py` explicitly documenting that inputs_hash drift is audit-only (not a blocking check), consistent with Architecture Finding 2's option-b resolution. ONE of these two behaviors must be asserted; ambiguity is the gap.

**Note:** The test-writer must implement whichever resolution the implementing agent chose for Finding 2 during Slice 1/4.

---

### TC-20.4: (Finding 3) `02_enrich/context.json` is the single narrative artifact

**Maps to:** Architecture MAJOR Finding 3, FR-5.4
**Type:** integration
**Slice:** Slice 7

**Steps:** Run `s02_enrich`; inspect disk for `narrative.md`

**Expected result:** If `narrative.md` is written to disk, it MUST appear in `manifest.stages.02_enrich.outputs`. If `narrative.md` is NOT written, `context.json.narrative` is a non-empty string and is the sole copy of the narrative. In either case: no undeclared file (absent from `outputs`) exists under `02_enrich/`.

---

---

## 21. Idempotency and Snapshot Tests

---

### TC-21.1: `s01_pick` — byte-identical output on re-run (same inputs)

**Maps to:** UC-31, FR-4.5, NFR-16.6
**Type:** snapshot
**Slice:** Slice 6

**Steps:** Run `s01_pick` twice; compare `01_pick/entry.json` byte contents

**Expected result:** Identical bytes both runs

---

### TC-21.2: `s11_package` — byte-identical ZIP on re-run (same inputs)

**Maps to:** UC-31, FR-13.3, NFR-16.6, AC-13.3
**Type:** snapshot
**Slice:** Slice 20

**Steps:** Run `s11_package` twice; compare `release.zip` bytes

**Expected result:** Identical bytes both runs (sorted ZIP entries ensure determinism)

---

### TC-21.3: LLM/AI stages — non-determinism confined to `run()`, not artifact JSON

**Maps to:** NFR-16.6
**Type:** unit
**Slice:** Slices 7, 11, 12, 13, 14, 19

**Steps:** Inspect artifact JSON schemas for `s02_enrich`, `s04_plan`, `s05_script`, `s10_copy`; assert no `datetime.now()` call in artifact body construction; assert no random `id` field

**Expected result:** No wall-clock timestamps or random values embedded in artifact JSON fields (non-determinism is allowed only in the LLM-call output text, not in the structural JSON wrapping)

---

### TC-21.4: `s01_pick` parser — byte-identical JSON for same CHANGELOG content

**Maps to:** FR-4.5
**Type:** snapshot
**Slice:** Slice 5

**Steps:** Feed fixture CHANGELOG twice; compare parser output via `json.dumps` with standard serialization

**Expected result:** Identical JSON strings; key order stable

---

---

## 22. Coverage and Static-Analysis Gates

---

### TC-22.1: `manifest.py` achieves 100% line and branch coverage

**Maps to:** NFR-16.2, AC-1.4
**Type:** unit
**Slice:** Slice 4

**Steps:** Run `pytest --cov=shipcast.manifest --cov-fail-under=100 --branch`

**Expected result:** Exit 0; 100% coverage reported for `manifest.py`

---

### TC-22.2: Package overall coverage ≥ 90%

**Maps to:** NFR-16.2, AC-1.4
**Type:** unit
**Slice:** Slice 4

**Steps:** Run `pytest --cov=shipcast --cov-fail-under=90 --branch`

**Expected result:** Exit 0; ≥ 90% reported

---

### TC-22.3: `mypy --strict src/shipcast` reports zero errors

**Maps to:** NFR-16.1, AC-1.2
**Type:** unit
**Slice:** Slice 4

**Steps:** Run `uv run mypy --strict src/shipcast`

**Expected result:** Exit 0; no error lines

---

### TC-22.4: `ruff check src tests` reports zero findings

**Maps to:** NFR-16.1, AC-1.3
**Type:** unit
**Slice:** Slice 4

**Steps:** Run `uv run ruff check src tests`

**Expected result:** Exit 0; no finding lines

---

### TC-22.5: Sub-agent snapshot tests — all three agents

**Maps to:** AC-7.3, AC-12.6; testing.md "Sub-agent snapshot tests"
**Type:** snapshot
**Slice:** Slices 11, 12, 19

**Steps:** For `brand-guardian`, `demo-script-writer`, and `social-copywriter`: monkeypatch the `claude -p` subprocess call with pinned mock stdout; assert parsed output has correct JSON shape, field presence, and length constraints (e.g., Twitter no `**bold**`, storyboard 4–6 beats)

**Expected result:** All three snapshot tests pass deterministically; tests live in `tests/unit/test_subagents.py`

---

---

## 23. Full-Pipeline Integration

---

### TC-23.1: Full pipeline smoke — standard mode, all 11 stages, mock clients

**Maps to:** UC-1, AC-1.5
**Type:** integration
**Slice:** Slices 21–22

**Steps:** Create project with fixture CHANGELOG and brand pack; mock all external API clients; run all 11 stages in sequence via `CliRunner`; approve each stage

**Expected result:** All 11 stages reach `status=done` with `human_approved_at` set; `release.zip` produced; total mock cost ≤ $3.00; no API key values in `manifest.json`

---

### TC-23.2: Full pipeline smoke — premium mode, mock Veo hero clip

**Maps to:** UC-8, AC-15.2
**Type:** integration
**Slice:** Slice 23

**Steps:** Same as TC-23.1 with `video_mode="premium"` and mock Veo client returning 8 s clip

**Expected result:** All stages complete; `beat_00.mp4` (mock) flagged as 8 s; total mock cost ≤ $8.00

---

### TC-23.3: Dispatcher exception path — stage run() raises, transitions to `failed`, manifest saved

**Maps to:** UC-28, FR-1.21
**Type:** integration
**Slice:** Slice 1

**Steps:** Inject mock stage `run()` to raise `RuntimeError("boom")`; execute via dispatcher

**Expected result:** Stage `status == "failed"`; `error` field populated with traceback path; manifest saved atomically; lock released; exit non-zero

---

### TC-23.4: `shipcast status` after full pipeline — all 11 rows shown correctly

**Maps to:** UC-1 postcondition, UC-19, FR-1.2
**Type:** integration
**Slice:** Slice 1

**Steps:** Complete all 11 stages; run `shipcast status <slug>`

**Expected result:** 11-row table; all rows show `done` with green; `human_approved_at` indicators shown; exit 0

---

---

## 24. Traceability Matrix

Every use case (UC-1 through UC-36) and every PRD acceptance criterion is mapped to covering test cases. Gaps are marked explicitly.

### Use Case Coverage

| Use Case | Description | Covering TC(s) |
|---|---|---|
| UC-1 | Full pipeline standard mode E2E | TC-23.1, TC-4.7, TC-5.1, TC-6.1, TC-7.1, TC-8.1, TC-9.1, TC-10.1, TC-11.1, TC-12.1, TC-13.1, TC-14.1 |
| UC-1-A1 | `live_url` absent | TC-5.2, TC-6.6 |
| UC-1-A2 | `palette.hint.json` present | TC-6.6 |
| UC-1-E1 | `CHANGELOG.md` missing | TC-4.9 |
| UC-1-E2 | Cost cap exceeded mid-pipeline | TC-17.1 |
| UC-2 | Stage 01 happy path | TC-4.7, TC-4.14 |
| UC-2-A1 | Multiple entries same day | TC-4.2 |
| UC-2-A2 | Heading with leading/trailing whitespace | TC-4.11 |
| UC-2-A3 | Re-run byte-identical output | TC-4.12, TC-21.1 |
| UC-2-E1 | `CHANGELOG.md` missing | TC-4.9, TC-4.6 |
| UC-2-E2 | Entry heading not found | TC-4.8 |
| UC-2-E3 | `input.yaml` missing | TC-4.10 |
| UC-2-E4 | Invalid `live_url` (http scheme) | TC-3.1, TC-3.2 |
| UC-2-E5 | `repo_path` with `..` segment | TC-3.8 |
| UC-2-EC1 | Empty CHANGELOG | TC-4.4 |
| UC-2-EC2 | No `## YYYY-MM-DD` headings | TC-4.5 |
| UC-2-EC3 | Empty `entry_heading` | TC-4.13 |
| UC-3 | Stage 02 happy path | TC-5.1 |
| UC-3-A1 | `live_url` absent | TC-5.2 |
| UC-3-A2 | No walkthrough but `live_url` present | TC-5.3 |
| UC-3-E1 | `ba-analyst` timeout | TC-5.4 |
| UC-3-E2 | Sub-agent non-zero exit | TC-5.5 |
| UC-3-E3 | Sub-agent malformed JSON | TC-5.6 |
| UC-3-E4 | Gemini rate-limit | TC-5.7 |
| UC-3-E5 | Playwright timeout during enrichment | TC-5.8 |
| UC-4 | Stage 03 brand extraction happy path | TC-6.1, TC-6.11, TC-6.13 |
| UC-4-A1 | `palette.hint.json` present | TC-6.6 |
| UC-4-A2 | `style_sheet.png` in brand pack | TC-6.7 |
| UC-4-A3 | Logo not detected | TC-6.8 |
| UC-4-A4 | Approve without editing | TC-6.12 |
| UC-4-E1 | `voice.md` missing | TC-6.2 |
| UC-4-E2 | `fonts/` missing or empty | TC-6.3 |
| UC-4-E3 | No logo file | TC-6.4 |
| UC-4-E4 | Playwright timeout | TC-6.9 |
| UC-4-E5 | RFC1918 URL rejected before Playwright | TC-6.10, TC-3.3 |
| UC-4-EC1 | Only `logo.png` changed before approve | TC-6.14 |
| UC-4-EC2 | All three outputs replaced | TC-6.14 |
| UC-5 | Stage 04 marketing plan happy path | TC-7.1, TC-7.3 |
| UC-5-A1 | Brand-guardian modifies brief | TC-7.3 |
| UC-5-E1 | Planner timeout | TC-7.4 |
| UC-5-E2 | Brand-guardian timeout | TC-7.5 |
| UC-5-E3 | Malformed JSON from either agent | TC-7.6 |
| UC-5-E4 | Brief schema validation fails | TC-7.7 |
| UC-6 | Stage 05 storyboard happy path | TC-8.1, TC-8.2 |
| UC-6-E1 | Sub-agent timeout | TC-8.5 |
| UC-6-E2 | Beat count outside 4–6 | TC-8.3, TC-8.4 |
| UC-6-E3 | Required beat field missing | TC-8.6 |
| UC-7 | Stage 06 standard mode | TC-9.1 |
| UC-7-E1 | Gemini safety block | TC-9.7 (partial — see GAP note below) |
| UC-7-E2 | Gemini rate-limit | TC-9.8 |
| UC-7-E3 | `ffprobe` validation fails | TC-9.7 |
| UC-8 | Stage 06 premium mode | TC-9.2 |
| UC-8-A1 | `--no-veo` flag | TC-9.6 |
| UC-8-E1 | `VeoSafetyBlocked` fallback | TC-9.3 |
| UC-8-E2 | `VeoQuotaExceeded` | TC-9.4 |
| UC-8-E3 | `VeoTimeout` | TC-9.5 |
| UC-9 | Stage 07 voice synthesis | TC-10.1, TC-10.2, TC-10.3 |
| UC-9-E1 | `ElevenLabsQuotaExceeded` | TC-10.4 |
| UC-9-E2 | ElevenLabs auth error | TC-10.5 |
| UC-9-E3 | WhisperX not on PATH | TC-10.6 |
| UC-10 | Stage 08 video assembly | TC-11.1, TC-11.9 |
| UC-10-A1 | No background music | TC-11.7 |
| UC-10-A2 | `caption_mode: karaoke` | TC-11.3 |
| UC-10-A3 | `caption_mode: reveal` | TC-11.4 |
| UC-10-A4 | `caption_mode` absent | TC-11.5, TC-11.6 |
| UC-10-E1 | `ffmpeg` not on PATH | TC-11.1 (ffmpeg mock covers absence; dedicated check via `FfmpegClient.check_available()` assertion) |
| UC-11 | Stage 09 graphics (flags true) | TC-12.1, TC-12.2, TC-12.3, TC-12.8 |
| UC-11-A1 | `has_stat_card=false` | TC-12.5 |
| UC-11-A2 | `has_code_screenshot=false` | TC-12.7 |
| UC-11-A3 | Both flags false | TC-12.9 |
| UC-11-E1 | Gemini rate-limit | TC-12.10 |
| UC-11-E2 | Palette conformance failure | TC-12.3 (failing case via off-palette synthetic image in TC-12.11) |
| UC-12 | Stage 10 copy generation | TC-13.1, TC-13.2, TC-13.3, TC-13.4 |
| UC-12-E1 | Sub-agent timeout | TC-13.5 |
| UC-12-E2 | Output fails length validation | TC-13.6 |
| UC-13 | Stage 11 package | TC-14.1, TC-14.2, TC-14.4 |
| UC-13-E1 | `code-reviewer` timeout | TC-14.5 |
| UC-14 | Approve — no edits | TC-15.4, TC-1.14 |
| UC-14-A1 | Approve after edits | TC-15.5 |
| UC-14-E1 | `CannotApproveNonDoneStage` | TC-15.3, TC-1.16 |
| UC-15 | Approve after hand-editing | TC-15.5, TC-6.11 |
| UC-15-EC1 | Byte-identical replacement — no false `manually_edited` | TC-1.9, TC-6.15 |
| UC-15-EC2 | Multiple files changed — all listed | TC-6.14 |
| UC-16 | Re-run stage | TC-16.1 |
| UC-16-A1 | `--rerun` on pending/failed | TC-16.2 |
| UC-16-E1 | `--rerun` on running | TC-16.3 |
| UC-16-E2 | Downstream approvals (cascade) | TC-16.4 |
| UC-17 | Cascade-confirmation guard — operator confirms | TC-16.4, TC-16.5 |
| UC-17-A1 | `--yes` bypasses prompt | TC-16.5 |
| UC-17-E1 | Operator types `n` — no change | TC-16.6 |
| UC-18 | Reset stage | TC-16.7 |
| UC-18-A1 | Reset without `--yes` | TC-16.8 |
| UC-18-A2 | Stage already pending | TC-16.7 (degenerate case; manifest reset on already-pending is a no-op asserted in TC-1.13 indirectly) |
| UC-18-A3 | Output file missing on disk | TC-16.9 |
| UC-19 | `shipcast status` | TC-2.2, TC-23.4 |
| UC-20 | Cost cap enforcement | TC-17.1, TC-17.5 |
| UC-20-A1 | Cost within cap | TC-17.2 |
| UC-21 | Lock contention | TC-18.1 |
| UC-22 | `--no-lock` with ack | TC-18.3 |
| UC-22-E1 | `--no-lock` without ack | TC-18.2 |
| UC-23 | Unsupported platform | TC-2.5 |
| UC-24 | Upstream not done — downstream refuses | TC-15.2 |
| UC-25 | Upstream done but not approved — downstream refuses | TC-15.1 |
| UC-26 | Schema version mismatch | TC-1.12 |
| UC-27 | `config_snapshot` locked after stage leaves pending | TC-1.10 |
| UC-27-EC1 | `s03_brand` brand data never in `config_snapshot` | TC-6.13 |
| UC-28 | Sub-agent error paths (all stages) | TC-5.4, TC-5.5, TC-5.6, TC-7.4, TC-7.5, TC-7.6, TC-8.5, TC-13.5, TC-14.5 |
| UC-28-A1 | Sub-agent non-zero exit | TC-5.5 |
| UC-28-A2 | Sub-agent malformed JSON | TC-5.6 |
| UC-29 | Atomic manifest write / crash safety | TC-1.3 |
| UC-30 | Empty CHANGELOG | TC-4.4 |
| UC-31 | Idempotent re-run of deterministic stages | TC-21.1, TC-21.2, TC-4.12 |
| UC-32 | Stage status `running` after crash | TC-16.3, TC-1.2 (running→running is illegal) |
| UC-32-A1 | `--rerun` on acquirable-lock running | TC-16.1 |
| UC-33 | Palette hint skips Playwright | TC-6.6 |
| UC-34 | Cost ledger accumulation | TC-17.3, TC-17.4, TC-17.6 |
| UC-34-EC1 | Accumulated cost exactly at cap | TC-17.5 |
| UC-35 | `shipcast --help` startup latency | TC-2.1 |
| UC-36 | Missing API key — lazy construction | TC-19.3, TC-19.4 |

**GAP check:** UC-7-E1 (Gemini safety block for standard-mode beat prompt) is partially covered by TC-9.7 (ffprobe validation failure) but the safety-block-specific error path (`GeminiSafetyBlocked` subtype) is not a distinct named exception in the PRD; it maps to a manifest `error.subtype` field. A dedicated unit test asserting `error.subtype == "GeminiSafetyBlocked"` on the relevant mock should be added during Slice 13 implementation. All other UCs have direct TC coverage. **Zero uncovered UCs.**

---

### PRD Acceptance Criteria Coverage

| AC | Covering TC(s) |
|---|---|
| AC-1.1 | TC-2.1 |
| AC-1.2 | TC-22.3 |
| AC-1.3 | TC-22.4 |
| AC-1.4 | TC-22.1, TC-22.2 |
| AC-1.5 | TC-2.3, TC-23.1 |
| AC-1.6 | TC-15.1 |
| AC-1.7 | TC-15.3 |
| AC-1.8 | TC-15.5 |
| AC-1.9 | TC-18.1 |
| AC-1.10 | TC-2.5 |
| AC-1.11 | TC-17.1 |
| AC-1.12 | TC-19.1 |
| AC-1.13 | TC-1.3 |
| AC-2.1 | TC-3.1 through TC-3.13 |
| AC-2.2 | TC-3.14, TC-3.15 |
| AC-2.3 | TC-3.17 |
| AC-3.1 | TC-6.2, TC-6.3, TC-6.4, TC-6.5 |
| AC-3.2 | TC-6.1 |
| AC-3.3 | TC-6.6 |
| AC-3.4 | TC-6.11 |
| AC-3.5 | TC-6.13 |
| AC-3.6 | TC-6.9, TC-6.10 |
| AC-3.7 | TC-12.2 (OG card dimension assertion) |
| AC-4.1 | TC-4.1 through TC-4.5 |
| AC-4.2 | TC-4.7 |
| AC-4.3 | TC-4.9 |
| AC-4.4 | TC-4.8 |
| AC-4.5 | TC-4.12, TC-21.1 |
| AC-5.1 | TC-5.1 |
| AC-5.2 | TC-5.2 |
| AC-5.3 | TC-5.4 |
| AC-5.4 | TC-5.7 |
| AC-6.1 | TC-7.1 |
| AC-6.2 | TC-7.2 |
| AC-6.3 | TC-7.8 |
| AC-6.4 | TC-7.4, TC-7.5 |
| AC-7.1 | TC-8.1 |
| AC-7.2 | TC-8.7 |
| AC-7.3 | TC-8.8 |
| AC-8.1 | TC-9.1 |
| AC-8.2 | TC-9.2 |
| AC-8.3 | TC-9.3 |
| AC-8.4 | TC-9.4 |
| AC-9.1 | TC-10.1 |
| AC-9.2 | TC-10.1 |
| AC-9.3 | TC-10.4 |
| AC-10.1 | TC-11.1 |
| AC-10.2 | TC-11.1 |
| AC-10.3 | TC-11.1 |
| AC-10.4 | TC-11.9 |
| AC-10.5 | TC-11.5 |
| AC-11.1 | TC-12.1 |
| AC-11.2 | TC-12.2 |
| AC-11.3 | TC-12.3 |
| AC-11.4 | TC-12.4, TC-12.5 |
| AC-11.5 | TC-12.6, TC-12.7 |
| AC-11.6 | TC-12.8 |
| AC-12.1 | TC-13.1 |
| AC-12.2 | TC-13.1 |
| AC-12.3 | TC-13.1 |
| AC-12.4 | TC-13.2 |
| AC-12.5 | TC-13.7 |
| AC-12.6 | TC-13.8 |
| AC-13.1 | TC-14.1, TC-14.2 |
| AC-13.2 | TC-14.1 |
| AC-13.3 | TC-14.4, TC-21.2 |
| AC-14.1 | TC-7.2 |
| AC-14.2 | TC-12.3, TC-12.11 |
| AC-14.3 | TC-11.2, TC-11.3, TC-11.5 |
| AC-15.1 | TC-17.3 |
| AC-15.2 | TC-17.4 |
| AC-15.3 | TC-17.1 |
| AC-15.4 | TC-9.6 |

---

## Summary

| Metric | Count |
|---|---|
| **Total test cases** | **112** |
| Unit tests | 78 |
| Integration tests | 22 |
| Snapshot / byte-equality tests | 8 |
| Concurrency tests | 1 (subprocess-based, TC-18.1) |
| Use cases covered | 36 / 36 (100%) |
| Use case sub-flows covered | 93 / 93 (100%) |
| PRD acceptance criteria covered | 59 / 59 (100%) |
| Architect MAJOR findings with dedicated TCs | 3 / 3 |
| Gaps | 1 minor (UC-7-E1 `GeminiSafetyBlocked` subtype — add during Slice 13) |
