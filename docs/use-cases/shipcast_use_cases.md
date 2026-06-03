# Use Cases: shipcast Auto-Marketing Pipeline

> Based on [PRD](../PRD.md) — All 17 sections, 101 FRs
> Reference plan: `/Users/aleksei/.claude/plans/okay-so-currently-i-unified-canyon.md`

This document is the authoritative source for E2E test scenarios. Use-case IDs (UC-N, UC-N-AN, UC-N-EN) are stable references mapped by `docs/qa/shipcast_test_cases.md`.

---

## Actor and System Glossary

- **Operator** — the single human user who drives the entire pipeline
- **Dispatcher** — the `shipcast` CLI's internal execution wrapper (owns manifest writes, lock, cost-gate, checklist print)
- **Stage** — one of the eleven concrete pipeline stages (`s01_pick` through `s11_package`)
- **Sub-agent** — a `claude -p <name>` subprocess invoked by a stage (`ba-analyst`, `planner`, `brand-guardian`, `demo-script-writer`, `social-copywriter`, `code-reviewer`)

---

## Primary Flows

---

### UC-1: Create a Project and Run the Full Pipeline (Standard Mode, End-to-End)

**Actor:** Operator
**Preconditions:**
- `shipcast` CLI is installed and on PATH (`uv run shipcast` resolves)
- Running on macOS or Linux
- `projects/_template/` exists in the repo
- `projects/_brand/<brand_slug>/` is populated with `voice.md`, `fonts/*.ttf`, and `logo.svg`
- Target repo at `repo_path` contains a valid `CHANGELOG.md` with the requested entry heading
- All required API keys are present in `.env` (`ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`)
- `ffmpeg` 8.x is on PATH
- `claude` CLI is on PATH
- `input.yaml` has `video_mode: standard` and a valid `live_url` (https, public host)
- No `palette.hint.json` is present in the brand pack (Playwright extraction will run)

**Trigger:** `shipcast pick <repo_path> --entry "<entry_heading>"`

### Primary Flow (Happy Path)

1. Operator runs `shipcast pick ../getdeal-platform-monorepo --entry "Add CSV export"` (FR-1.1)
2. CLI validates platform is macOS or Linux (FR-1.31); proceeds
3. CLI reads `input.yaml`; `InputYaml` Pydantic model validates all fields including `live_url` (https, non-RFC1918) and `repo_path` (under allowed root, contains `CHANGELOG.md`) (FR-2.1, FR-2.3, FR-2.4)
4. CLI calls `Project.create`, copies `_template/`, writes `manifest.json` with all eleven stages `pending` and `config_snapshot` from `config.toml`; no API keys in `config_snapshot` (FR-1.1, FR-1.4, FR-1.11, FR-1.29)
5. Dispatcher acquires `fcntl.flock(LOCK_EX|LOCK_NB)` on `projects/<slug>/.lock` (FR-1.30)
6. Dispatcher transitions `01_pick` to `running`, calls `s01_pick.check_inputs()` (passes), calls `s01_pick.run()`: parses `CHANGELOG.md`, locates "Add CSV export" entry, writes `01_pick/entry.json` (FR-4.1–FR-4.4)
7. Dispatcher computes `outputs_hash_at_done`, transitions to `done`, saves manifest, releases lock, prints Review Checklist (FR-1.20)
8. Operator reviews `01_pick/entry.json`, runs `shipcast approve getdeal--csv-export 01_pick` (FR-1.14); `manually_edited=false` recorded
9. Operator runs `shipcast enrich getdeal--csv-export`; dispatcher acquires lock, `s02_enrich.check_inputs()` confirms `01_pick` is done and approved; `run()` executes: `gh pr list` + `git log`, Playwright walkthrough of `live_url`, `gemini_client.multimodal()` call, `ba-analyst` sub-agent invocation; writes `02_enrich/context.json` (FR-5.1–FR-5.4)
10. Operator approves `02_enrich`
11. Operator runs `shipcast brand getdeal--csv-export`; `s03_brand.check_inputs()` validates brand pack completeness (FR-3.3); `run()` calls `playwright_client.extract_css_palette()`, `extract_font_family()`, `screenshot_logo()`; calls `gemini_client.generate_image(aspect_ratio="1:1")` for `style_sheet.png`; writes `03_brand/proposal.json`, `logo.png`, `style_sheet.png` (FR-3.4–FR-3.8)
12. Operator edits `03_brand/proposal.json` to reduce palette to exactly three hex codes (`primary`, `accent`, `neutral`) (FR-3.9); runs `shipcast approve getdeal--csv-export 03_brand`; dispatcher detects hash mismatch and records `manually_edited=true` (FR-3.10)
13. Operator runs `shipcast plan getdeal--csv-export`; `s04_plan.run()` calls `planner` sub-agent then `brand-guardian` sub-agent sequentially; writes `04_plan/brief.json` with `video_beats` length=4, `carousel_beats` length=4, `has_stat_card`, `has_code_screenshot` flags (FR-6.1–FR-6.6)
14. Operator approves `04_plan`
15. Operator runs `shipcast script getdeal--csv-export`; `s05_script.run()` calls `demo-script-writer` sub-agent; writes `05_script/storyboard.json` with 4–6 beats (FR-7.1–FR-7.4)
16. Operator approves `05_script`
17. Operator runs `shipcast video_assets getdeal--csv-export`; standard mode: four beats, each rendered via `gemini_client.generate_image(aspect_ratio="9:16")` + `ffmpeg_client.ken_burns_clip()`; writes `06_video_assets/beat_00.mp4` through `beat_03.mp4` (FR-8.1, FR-8.2)
18. Operator approves `06_video_assets`
19. Operator runs `shipcast voice getdeal--csv-export`; `s07_voice.run()` joins narration lines, sends to ElevenLabs, runs WhisperX; writes `07_voice/narration.mp3` and `07_voice/words.json` (FR-9.1–FR-9.4)
20. Operator approves `07_voice`
21. Operator runs `shipcast video getdeal--csv-export`; `s08_video.run()` calls `_assemble_raw()` (concat + narration + optional bgm), `_overlay_captions()` (chip mode by default), `_export_loop()`; writes `08_video/showcase.mp4`, `loop_6s.mp4`, `loop_6s.gif` (FR-10.1–FR-10.5)
22. Operator approves `08_video`
23. Operator runs `shipcast graphics getdeal--csv-export`; `s09_graphics.run()` produces 4 aspect-ratio cards, OG card, conditional stat card and code screenshot, and LinkedIn carousel (6 slides) (FR-11.1–FR-11.9)
24. Operator approves `09_graphics`
25. Operator runs `shipcast copy getdeal--csv-export`; `s10_copy.run()` calls `social-copywriter` sub-agent; writes `10_copy/twitter_thread.md`, `linkedin.md`, `blog.md` (FR-12.1–FR-12.5)
26. Operator approves `10_copy`
27. Operator runs `shipcast package getdeal--csv-export`; `s11_package.run()` assembles `release.zip` and writes `11_package/README.md`; calls `code-reviewer` sub-agent for README link sanity check (FR-13.1–FR-13.5)
28. Operator approves `11_package`; pipeline complete

**Postconditions:**
- All eleven stage records have `status=done` and `human_approved_at` non-null
- `11_package/release.zip` contains all required output artifacts
- `11_package/README.md` has three fenced code blocks and an asset table
- Total accumulated `cost_usd` across all stages is ≤ $3.00 (standard mode cap)
- No API key values appear in `manifest.json` or any log file

### Alternative Flows

- **UC-1-A1: `live_url` absent** — Playwright sub-steps in `s02_enrich` and `s03_brand` are skipped; `s02_enrich/context.json` has `screenshots: []`; `s03_brand` still writes `proposal.json` using brand-pack fonts and `palette.hint.json` if present, or fails with `BrandPackIncomplete` if no palette source; see UC-3-A1 and UC-5-A1 for the detailed per-stage treatments
- **UC-1-A2: `palette.hint.json` present in brand pack** — `s03_brand` skips `playwright_client.extract_css_palette()` entirely; uses the pre-seeded `{primary, accent, neutral}` values directly; see UC-5-A2

### Error Flows

- **UC-1-E1: `CHANGELOG.md` missing** — `s01_pick.run()` raises `ChangelogFileMissing`; stage transitions to `failed`; CLI exits non-zero; CHANGELOG is never auto-created; see UC-2-E1
- **UC-1-E2: Cost cap exceeded mid-pipeline** — see UC-20

### Data Requirements

- **Input:** `input.yaml`, `CHANGELOG.md` in target repo, brand pack files, API keys in `.env`
- **Output:** `01_pick/entry.json` through `11_package/release.zip` + `README.md`
- **Manifest fields written:** all eleven `StageRecord` fields across eleven stages
- **Side effects:** eleven stage directories populated; `manifest.json` updated eleven times; eleven log files written; eleven lock acquisition/release cycles

---

### UC-2: Run Stage 01 — Pick Changelog Entry

**Actor:** Operator
**Preconditions:**
- `projects/<slug>/` exists with `manifest.json` (all stages `pending`)
- `projects/<slug>/input.yaml` is valid
- `01_pick` has `status=pending`
- Target repo has a `CHANGELOG.md` containing the requested entry heading

**Trigger:** `shipcast pick <slug>`

### Primary Flow (Happy Path)

1. Operator runs `shipcast pick getdeal--csv-export` (FR-1.1)
2. Dispatcher loads project, acquires lock, initializes logging (FR-1.30, FR-1.33)
3. Dispatcher transitions `01_pick` to `running`, saves manifest atomically (FR-1.8, FR-1.9)
4. `s01_pick.check_inputs()` verifies `input.yaml` is present and readable; passes
5. `s01_pick.run()`: reads `input.yaml`, invokes `InputYaml` Pydantic validation, calls `changelog/parser.py` to parse target `CHANGELOG.md` into `list[ChangelogEntry]` (FR-4.1), locates entry by case-insensitive heading match (FR-4.3), writes `01_pick/entry.json` with deterministic JSON serialization (FR-4.4)
6. `validate_outputs()` confirms `entry.json` passes `ChangelogEntry` schema (FR-1.23)
7. Dispatcher computes `outputs_hash_at_done`, transitions to `done`, saves manifest, releases lock (FR-1.13)
8. Dispatcher prints Review Checklist with absolute path to `01_pick/entry.json`, three checklist bullets, and operator instructions for `--rerun`, hand-edit-then-approve, and `reset` (FR-1.22)
9. CLI exits 0

**Postconditions:**
- `01_pick/entry.json` exists and validates against `ChangelogEntry` schema
- `manifest.stages.01_pick.status = "done"`, `outputs_hash_at_done` populated, `human_approved_at = null`

### Alternative Flows

- **UC-2-A1: Multiple entries same day in CHANGELOG** — parser returns all entries; heading match is exact (case-insensitive, trimmed); the correct entry is selected; if two entries have identical headings, the first match is used (FR-4.1–FR-4.3)
- **UC-2-A2: Entry heading has leading/trailing whitespace in `input.yaml`** — `entry_heading` is trimmed before comparison; match succeeds (FR-4.3)
- **UC-2-A3: Re-run on identical inputs produces byte-identical output** — `s01_pick` is deterministic; `entry.json` bytes are identical on two runs against the same `input.yaml` and unchanged `CHANGELOG.md` (FR-4.5, NFR-16.6)

### Error Flows

- **UC-2-E1: `CHANGELOG.md` missing from target repo** — parser raises `ChangelogFileMissing`; stage transitions to `failed`; `error.type = "ChangelogFileMissing"`; CLI exits non-zero; CHANGELOG is never auto-created (FR-4.2)
- **UC-2-E2: Entry heading not found in CHANGELOG** — `s01_pick.run()` raises `ChangelogEntryNotFound`; stage transitions to `failed`; `error.type = "ChangelogEntryNotFound"`; CLI exits non-zero (FR-4.3)
- **UC-2-E3: `input.yaml` missing** — `check_inputs()` raises `StageInputMissing`; stage fails before `run()` is called (FR-1.24)
- **UC-2-E4: `input.yaml` has invalid `live_url` (http scheme)** — `InputYaml` Pydantic validation raises `ValueError`; stage transitions to `failed`; error lists the failing field (FR-2.3, FR-2.7)
- **UC-2-E5: `input.yaml` has `repo_path` with `..` segment** — validation raises `ValueError`; stage fails (FR-2.4)

### Edge Cases

- **UC-2-EC1:** Empty `CHANGELOG.md` — parser returns `[]`; `ChangelogEntryNotFound` raised immediately; stage fails
- **UC-2-EC2:** CHANGELOG exists but has no `## YYYY-MM-DD` headings — parser returns `[]`; same outcome as EC1
- **UC-2-EC3:** `entry_heading` is an empty string in `input.yaml` — Pydantic `min_length` validation rejects it; `ValidationError` transitions stage to `failed`

### Data Requirements

- **Input:** `input.yaml`, `CHANGELOG.md` in `repo_path`
- **Output:** `01_pick/entry.json` (ChangelogEntry schema)
- **Manifest fields written:** `status`, `outputs`, `inputs_hash`, `outputs_hash_at_done`, `started_at`, `finished_at`
- **Side effects:** lock acquired/released; log file written; no external API calls

---

### UC-3: Run Stage 02 — Enrich Context

**Actor:** Operator
**Preconditions:**
- `01_pick` is `done` and `human_approved_at` is non-null
- `input.yaml` has a valid `live_url` (https, public host) and `feature_walkthrough` steps
- `GEMINI_API_KEY` and `ANTHROPIC_API_KEY` are set

**Trigger:** `shipcast enrich <slug>`

### Primary Flow (Happy Path)

1. Operator runs `shipcast enrich getdeal--csv-export`
2. Dispatcher acquires lock; `s02_enrich.check_inputs()` confirms `01_pick` done+approved and `01_pick/entry.json` exists (FR-1.24)
3. `s02_enrich.run()` executes three sub-steps in sequence:
   a. `gh pr list --json` and `git log --stat` in `repo_path` → `pr_links: list[str]`, `diff_stats: dict`
   b. Playwright walkthrough per `input_yaml.feature_walkthrough` steps → screenshots saved to `02_enrich/screenshots/` (FR-5.1)
   c. `gemini_client.multimodal(prompt, images)` with screenshots + diff stats + entry text → `02_enrich/narrative.md` (FR-5.5)
4. `ba-analyst` sub-agent invoked via `subprocess.run(["claude", "-p", "ba-analyst", ...], timeout=300)` for high-level framing (FR-5.3)
5. `s02_enrich` writes `02_enrich/context.json` with `pr_links`, `diff_stats`, `narrative`, `screenshots` (FR-5.4)
6. Dispatcher computes hash, transitions to `done`, prints Review Checklist; exits 0

**Postconditions:**
- `02_enrich/context.json` validates against `EnrichedContext` schema
- `02_enrich/screenshots/` contains ≥ 1 PNG
- `manifest.stages.02_enrich.status = "done"`

### Alternative Flows

- **UC-3-A1: `live_url` absent from `input.yaml`** — Playwright sub-step (b) is skipped and logged; `screenshots: []` in `context.json`; Gemini multimodal call (c) proceeds with only diff stats + entry text; `narrative` is still non-empty (FR-5.2)
- **UC-3-A2: `feature_walkthrough` absent but `live_url` present** — Playwright navigates to `live_url` and captures a single viewport screenshot; no step-by-step automation

### Error Flows

- **UC-3-E1: `ba-analyst` sub-agent times out (>300 s)** — stage raises `SubagentTimeout`; transitions to `failed`; `error.type = "SubagentTimeout"`; stderr captured in `error.message`; operator reruns (FR-5.3)
- **UC-3-E2: `ba-analyst` exits non-zero** — stage fails; stderr captured in `error`; exits non-zero (FR-5.3)
- **UC-3-E3: Sub-agent stdout is not valid JSON** — stage raises `SubagentMalformedOutput`; transitions to `failed` (FR-5.3)
- **UC-3-E4: Gemini multimodal returns rate-limit error** — stage raises `GeminiRateLimited`; transitions to `failed`; operator waits and reruns
- **UC-3-E5: Playwright navigation times out during enrichment screenshots** — stage raises `PlaywrightTimeout`; transitions to `failed`; operator confirms URL and reruns (FR-3.5)

### Data Requirements

- **Input:** `01_pick/entry.json`, `input.yaml` (`live_url`, `feature_walkthrough`, `repo_path`)
- **Output:** `02_enrich/context.json`, `02_enrich/screenshots/*.png`
- **Side effects:** `gh` and `git log` run in target repo; Playwright browser launched; Gemini API call; `claude -p` subprocess

---

### UC-4: Run Stage 03 — Brand Extraction

**Actor:** Operator
**Preconditions:**
- `02_enrich` is `done` and approved
- `projects/_brand/<brand_slug>/` contains `voice.md`, `fonts/*.ttf`, `logo.svg`
- No `palette.hint.json` in brand pack (full Playwright extraction path)
- `GEMINI_API_KEY` set

**Trigger:** `shipcast brand <slug>`

### Primary Flow (Happy Path)

1. Operator runs `shipcast brand getdeal--csv-export`
2. `s03_brand.check_inputs()` verifies `02_enrich` done+approved; calls `additional_input_paths()` to verify brand pack files exist: `voice.md`, at least one `.ttf` in `fonts/`, `logo.svg` or `logo.png` (FR-3.3, FR-1.25)
3. `s03_brand.run()`:
   a. Calls `InputYaml` URL validator on `live_url` before any Playwright call (FR-3.6)
   b. `playwright_client.extract_css_palette(live_url)` → top-5 hex colors (FR-3.4)
   c. `playwright_client.extract_font_family(live_url)` → font-family string (FR-3.4)
   d. `playwright_client.screenshot_logo(live_url)` → bytes or `None`; if `None`, writes 1×1 transparent PNG, sets `logo_detected=false` (FR-3.4)
   e. `gemini_client.generate_image(prompt, aspect_ratio="1:1")` → `03_brand/style_sheet.png` (FR-3.7)
   f. Writes `03_brand/proposal.json` (`BrandProposal` schema: `palette`, `font_family`, `logo_detected`) (FR-3.8)
4. Dispatcher computes `outputs_hash_at_done`, transitions to `done`, prints Review Checklist; exits 0
5. Operator edits `03_brand/proposal.json`: reduces palette from 5 candidates to exactly 3 (`primary`, `accent`, `neutral`); optionally replaces `logo.png` or `style_sheet.png` (FR-3.9)
6. Operator runs `shipcast approve getdeal--csv-export 03_brand`; dispatcher recomputes hash, detects mismatch, records `manually_edited=true`, lists changed files (FR-3.10)

**Postconditions:**
- `03_brand/proposal.json`, `03_brand/logo.png`, `03_brand/style_sheet.png` exist
- `manifest.stages.03_brand.manually_edited = true` (because proposal.json was edited)
- `manifest.json` `config_snapshot` is byte-identical to pre-run snapshot (brand data never in `config_snapshot`) (FR-3.11)

### Alternative Flows

- **UC-4-A1: `palette.hint.json` present in brand pack** — `extract_css_palette()` is never called; `proposal.json` is written with the hint's three values; Playwright palette extraction is skipped entirely (FR-3.2, FR-3.3)
- **UC-4-A2: `style_sheet.png` present in brand pack** — Gemini `generate_image()` call is skipped; operator-supplied image is used (FR-3.2)
- **UC-4-A3: Logo not detected (no matching selector)** — `screenshot_logo()` returns `None`; `03_brand/logo.png` is a 1×1 transparent PNG; `proposal.json` has `logo_detected=false`; operator is expected to supply a replacement before approving (FR-3.4)
- **UC-4-A4: Operator approves `03_brand` without editing** — hash matches; `manually_edited=false`; downstream stages are unblocked (UC-7 standard approve flow)

### Error Flows

- **UC-4-E1: Brand pack missing `voice.md`** — `check_inputs()` raises `BrandPackIncomplete` listing missing file(s); no external API call is made (FR-3.3)
- **UC-4-E2: Brand pack missing `fonts/` directory or contains no `.ttf`** — `BrandPackIncomplete` raised (FR-3.3)
- **UC-4-E3: Brand pack missing `logo.svg` and `logo.png`** — `BrandPackIncomplete` raised (FR-3.1, FR-3.3)
- **UC-4-E4: Playwright navigation timeout during brand extraction** — `playwright_client` raises `PlaywrightTimeout` (60 s limit); stage transitions to `failed`; operator confirms URL and reruns (FR-3.5)
- **UC-4-E5: `live_url` is an RFC1918 address** — URL validator rejects before Playwright is called; stage fails with `ValidationError`; no Playwright navigation occurs (FR-3.6, FR-2.3)

### Edge Cases

- **UC-4-EC1:** Operator edits only `logo.png` bytes (not `proposal.json`) before approving — `compute_outputs_hash` detects the byte change in `logo.png`; `manually_edited=true`; changed file is listed
- **UC-4-EC2:** All three output files replaced by operator — all three appear in the changed-files list; `manually_edited=true`

### Data Requirements

- **Input:** `02_enrich/context.json`, `input.yaml` (`live_url`, `brand_slug`), brand pack files
- **Output:** `03_brand/proposal.json`, `03_brand/logo.png`, `03_brand/style_sheet.png`
- **Side effects:** Playwright browser launched; Gemini image generation API call; brand data never written to `config_snapshot`

---

### UC-5: Run Stage 04 — Marketing Plan

**Actor:** Operator
**Preconditions:**
- `01_pick`, `02_enrich`, `03_brand` are all `done` and approved
- `ANTHROPIC_API_KEY` set; `~/.claude/agents/planner.md` and `~/.claude/agents/brand-guardian.md` exist

**Trigger:** `shipcast plan <slug>`

### Primary Flow (Happy Path)

1. Operator runs `shipcast plan getdeal--csv-export`
2. `s04_plan.check_inputs()` verifies all three upstream stages done+approved; verifies their output files exist (FR-1.24)
3. `s04_plan.run()`:
   a. Calls `planner` sub-agent via `claude -p` (300 s timeout) with `01_pick/entry.json` and `02_enrich/context.json` as context → draft `MarketingBrief` JSON (FR-6.1)
   b. Calls `brand-guardian` sub-agent via `claude -p` (300 s timeout) with the draft brief + `03_brand/proposal.json` + `03_brand/voice.md` → final `MarketingBrief` JSON (FR-6.2)
4. Final brief is Pydantic-validated against `MarketingBrief` schema: `hook_template_per_channel` (each value in the 7-item catalog), `ctas` (≥1), `video_beats` (exactly 4), `carousel_beats` (exactly 4), `has_stat_card`, `has_code_screenshot` (FR-6.4)
5. `s04_plan` writes `04_plan/brief.json` (FR-6.6)
6. Dispatcher transitions to `done`, prints Review Checklist; exits 0

**Postconditions:**
- `04_plan/brief.json` validates against `MarketingBrief` schema
- `video_beats` length == 4; `carousel_beats` length == 4
- Each `hook_template_per_channel` value is one of the 7 catalog keys

### Alternative Flows

- **UC-5-A1: `brand-guardian` modifies the brief** — the final output differs from the draft; both sub-agent invocations are treated as sequential black-boxes; only the final JSON (brand-guardian's output) is written to `brief.json` (FR-6.2)

### Error Flows

- **UC-5-E1: `planner` sub-agent times out** — `SubagentTimeout` raised; stage fails (FR-6.3)
- **UC-5-E2: `brand-guardian` sub-agent times out** — same outcome (FR-6.3)
- **UC-5-E3: Either sub-agent returns malformed JSON** — `SubagentMalformedOutput` raised; stage fails (FR-6.3)
- **UC-5-E4: Brief schema validation fails** — `validate_outputs()` raises; stage fails with structured error listing invalid fields

### Data Requirements

- **Input:** `01_pick/entry.json`, `02_enrich/context.json`, `03_brand/proposal.json`, `03_brand/voice.md`
- **Output:** `04_plan/brief.json`
- **Side effects:** two sequential `claude -p` subprocess invocations

---

### UC-6: Run Stage 05 — Script and Storyboard

**Actor:** Operator
**Preconditions:**
- `04_plan` is `done` and approved; `04_plan/brief.json` exists
- `~/.claude/agents/demo-script-writer.md` exists

**Trigger:** `shipcast script <slug>`

### Primary Flow (Happy Path)

1. `s05_script.check_inputs()` verifies `04_plan` done+approved and `brief.json` exists
2. `s05_script.run()` invokes `demo-script-writer` sub-agent via `claude -p` (300 s timeout) with `04_plan/brief.json` and `01_pick/entry.json` as context (FR-7.1)
3. Sub-agent returns `Storyboard` JSON with `beats: list[StoryboardBeat]`; each beat has `image_prompt`, `narration`, `duration_sec` (FR-7.2)
4. Beat count validated: must be between 4 and 6 inclusive; otherwise `SubagentMalformedOutput` (FR-7.3)
5. `s05_script` writes `05_script/storyboard.json` (FR-7.4)
6. Dispatcher transitions to `done`, prints Review Checklist

**Postconditions:**
- `05_script/storyboard.json` has 4–6 beats, each with `image_prompt`, `narration`, `duration_sec`

### Error Flows

- **UC-6-E1: Sub-agent times out** — `SubagentTimeout` raised; stage fails
- **UC-6-E2: Beat count outside 4–6 range** — `SubagentMalformedOutput` raised; stage fails
- **UC-6-E3: Required beat field missing** — Pydantic validation raises; `validate_outputs()` fails; stage fails with `StageOutputInvalid`

### Data Requirements

- **Input:** `04_plan/brief.json`, `01_pick/entry.json`
- **Output:** `05_script/storyboard.json`
- **Side effects:** one `claude -p` subprocess

---

### UC-7: Run Stage 06 — Video Assets (Standard Mode)

**Actor:** Operator
**Preconditions:**
- `05_script` is `done` and approved; `04_plan/brief.json` `video_mode = "standard"`
- `GEMINI_API_KEY` set; `ffmpeg` on PATH

**Trigger:** `shipcast video_assets <slug>`

### Primary Flow (Happy Path — Standard Mode)

1. `s06_video_assets.check_inputs()` verifies `05_script` done+approved; verifies `brief.json` `video_mode = "standard"`
2. `s06_video_assets.run()` iterates `brief.video_beats` (4 beats):
   - For each beat: `gemini_client.generate_image(beat.image_prompt, aspect_ratio="9:16")` → PNG still (FR-8.2)
   - `ffmpeg_client.ken_burns_clip(still, duration=beat.duration_sec)` → 1080×1920 h264 MP4 (FR-8.2)
3. Four clips written: `06_video_assets/beat_00.mp4` through `beat_03.mp4` (FR-8.7)
4. Each clip validated via `ffprobe` for codec (h264) and dimensions (1080×1920) (FR-8.7)
5. Dispatcher transitions to `done`; prints Review Checklist

**Postconditions:**
- Four MP4 clips in `06_video_assets/`, each 3–5 s, 1080×1920, h264 codec
- No Veo client invoked

### Alternative Flows

- **UC-7-A1: Premium mode — Veo hero clip** — see UC-8

### Error Flows

- **UC-7-E1: Gemini Imagen returns safety block for a beat's prompt** — stage records `error.subtype = "GeminiSafetyBlocked"`; operator edits `storyboard.json` and reruns
- **UC-7-E2: Gemini Imagen rate-limit error** — `GeminiRateLimited` raised; stage fails; operator waits and reruns
- **UC-7-E3: `ffprobe` validation fails (wrong codec or dimensions)** — stage fails with structured error identifying the malformed clip

### Data Requirements

- **Input:** `05_script/storyboard.json`, `04_plan/brief.json`
- **Output:** `06_video_assets/beat_{00..03}.mp4`
- **Side effects:** Gemini Imagen API calls (4); `ffmpeg` subprocesses (4)

---

### UC-8: Run Stage 06 — Video Assets (Premium Mode with Veo Hero Clip)

**Actor:** Operator
**Preconditions:**
- `input.yaml` has `video_mode: premium`
- All conditions from UC-7 apply
- `GEMINI_API_KEY` covers Veo 3 Fast quota

**Trigger:** `shipcast video_assets <slug>` with premium-mode project

### Primary Flow (Happy Path — Premium Mode)

1. `s06_video_assets.run()` reads `video_mode = "premium"` from `config_snapshot`
2. Beat[0]: calls `veo_client.generate_clip(beat.image_prompt, conditioning_image="03_brand/style_sheet.png")` → polls until job completes (up to 120 s) → 8 s MP4 (FR-8.3, FR-8.4)
3. Beats[1..3]: same Gemini Imagen + Ken-Burns path as standard mode (FR-8.3)
4. Four clips written; beat[0] is 8 s, beats[1..3] are 3–5 s (FR-8.7)
5. Clips validated via `ffprobe`; Dispatcher transitions to `done`

**Postconditions:**
- `beat_00.mp4` is 8 s ± 0.1 s, 1080×1920, h264
- `beat_01.mp4` through `beat_03.mp4` are 3–5 s, 1080×1920, h264
- Accumulated cost includes $3.20 Veo 3 Fast charge

### Alternative Flows

- **UC-8-A1: `--no-veo` flag** — beat[0] uses Imagen + Ken-Burns path; Veo client is never called; all four clips are 3–5 s; cost stays within standard-mode range (FR-15.4)

### Error Flows

- **UC-8-E1: `VeoSafetyBlocked` on beat[0]** — stage silently falls back to Imagen + Ken-Burns for beat[0]; safety-blocked prompt is NOT written to any log file; clip produced normally (FR-8.5)
- **UC-8-E2: `VeoQuotaExceeded`** — stage fails immediately; no subsequent beats are processed; operator waits for quota reset and reruns (FR-8.6)
- **UC-8-E3: `VeoTimeout` (>120 s polling)** — stage fails with `VeoTimeout`; operator reruns

### Data Requirements

- **Input:** `05_script/storyboard.json`, `04_plan/brief.json`, `03_brand/style_sheet.png` (Veo conditioning)
- **Output:** `06_video_assets/beat_{00..03}.mp4`
- **Side effects:** Veo 3 Fast REST call (beat[0]); Gemini Imagen calls (beats[1..3]); cost ≈ $3.20 (Veo) + $0.12 (3 × Imagen)

---

### UC-9: Run Stage 07 — Voice Synthesis

**Actor:** Operator
**Preconditions:**
- `05_script` is `done` and approved; `05_script/storyboard.json` exists
- `ELEVENLABS_API_KEY` set; `whisperx` is available on PATH

**Trigger:** `shipcast voice <slug>`

### Primary Flow (Happy Path)

1. `s07_voice.check_inputs()` verifies `05_script` done+approved
2. `s07_voice.run()`:
   a. Joins `beat.narration` strings from `storyboard.json` beats with single newlines (FR-9.1)
   b. Sends joined text to `elevenlabs_client.synthesize(text, voice_id=settings.voice_id)` → MP3 bytes (FR-9.2)
   c. Saves `07_voice/narration.mp3` (FR-9.3)
   d. Calls `whisperx_client.transcribe("07_voice/narration.mp3")` → `{word, start, end}` array (FR-9.4)
   e. Saves `07_voice/words.json` (FR-9.4)
3. Dispatcher transitions to `done`; prints Review Checklist

**Postconditions:**
- `07_voice/narration.mp3` exists; `ffprobe` reports valid MP3; duration within ±1 s of sum of `beat.duration_sec`
- `07_voice/words.json` is non-empty; word durations sum within 1 s of MP3 duration

### Error Flows

- **UC-9-E1: ElevenLabs 429 quota exceeded** — stage raises `ElevenLabsQuotaExceeded`; transitions to `failed`; no files written; operator waits and reruns (FR-9.5)
- **UC-9-E2: ElevenLabs auth error (bad API key)** — `MissingApiKey` or HTTP 401 captured; stage fails
- **UC-9-E3: WhisperX not available on PATH** — stage fails with descriptive error before any synthesis

### Data Requirements

- **Input:** `05_script/storyboard.json`, `Settings.voice_id`
- **Output:** `07_voice/narration.mp3`, `07_voice/words.json`
- **Side effects:** ElevenLabs API call; WhisperX local transcription

---

### UC-10: Run Stage 08 — Video Assembly

**Actor:** Operator
**Preconditions:**
- `06_video_assets` and `07_voice` are both `done` and approved
- `ffmpeg` on PATH

**Trigger:** `shipcast video <slug>`

### Primary Flow (Happy Path)

1. `s08_video.check_inputs()` verifies `06_video_assets` and `07_voice` done+approved
2. `s08_video.run()`:
   a. `_assemble_raw()`: concatenates `beat_{00..03}.mp4` with `narration.mp3` as primary audio; if `_brand/<slug>/music/*.mp3` exists (first alphabetically), mixes in bgm with narration ducked to −3 dB (FR-10.1)
   b. `_overlay_captions()`: reads `caption_mode:` from `03_brand/voice.md`; applies the matching renderer (`chip`, `karaoke`, or `reveal`); default `chip` if line absent or unrecognized (FR-10.2)
   c. `_export_loop()`: takes first 6 s of beat[0], center-crops to 1080×1080, removes audio; exports `08_video/loop_6s.mp4` and `08_video/loop_6s.gif` (FR-10.4)
3. Writes `08_video/showcase.mp4`, `08_video/loop_6s.mp4`, `08_video/loop_6s.gif` (FR-10.5)
4. Dispatcher transitions to `done`; prints Review Checklist

**Postconditions:**
- `showcase.mp4`: 15–25 s, 1080×1920, h264+aac
- `loop_6s.mp4`: 6.0 s ± 0.1 s, 1080×1080, no audio
- `loop_6s.gif`: ≤ 8 MB

### Alternative Flows

- **UC-10-A1: No background music in brand pack** — `_assemble_raw()` uses only narration as audio track; no ducking applied; output is valid
- **UC-10-A2: `caption_mode: karaoke` in `voice.md`** — karaoke renderer used; word-by-word highlight captions
- **UC-10-A3: `caption_mode: reveal` in `voice.md`** — reveal renderer used
- **UC-10-A4: `caption_mode` line absent or unrecognized value** — `chip` renderer used (FR-14.8)

### Error Flows

- **UC-10-E1: `ffmpeg` not on PATH** — dispatcher checks `FfmpegClient.check_available()` before acquiring lock; raises `FfmpegNotFound` with install instructions; no manifest transition

### Data Requirements

- **Input:** `06_video_assets/beat_{00..03}.mp4`, `07_voice/narration.mp3`, `07_voice/words.json`, `03_brand/voice.md`, optional bgm files
- **Output:** `08_video/showcase.mp4`, `08_video/loop_6s.mp4`, `08_video/loop_6s.gif`
- **Side effects:** `ffmpeg` subprocesses; PIL caption rendering

---

### UC-11: Run Stage 09 — Static Graphics

**Actor:** Operator
**Preconditions:**
- `04_plan`, `03_brand` are `done` and approved; `04_plan/brief.json` and brand files exist
- `GEMINI_API_KEY` set

**Trigger:** `shipcast graphics <slug>`

### Primary Flow (Happy Path — `has_stat_card=true`, `has_code_screenshot=true`)

1. `s09_graphics.check_inputs()` verifies upstream stages done+approved
2. `s09_graphics.run()` calls:
   a. `_render_aspect_card(ratio)` for each of `1x1`, `16x9`, `9x16`, `4x5`: `gemini_client.generate_image(prompt, aspect_ratio=ratio)` + PIL headline overlay with brand display font (FR-11.1)
   b. `_render_og()`: `gemini_client.generate_image(prompt, aspect_ratio="og")` → 1200×630 image + entry name + brand logo overlay (FR-11.3)
   c. `_render_stat(ratio)` for four ratios (because `brief.has_stat_card=true`) → `09_graphics/stat_{1x1,16x9,9x16,4x5}.png` (FR-11.4)
   d. `_render_code()` (because `brief.has_code_screenshot=true`) → Pygments + PIL Ray.so-style `09_graphics/code.png` (FR-11.6)
   e. `_render_carousel_slide(idx, beat)` for 6 slides → `09_graphics/carousel/slide_{01..06}.png` (FR-11.8)
3. Each aspect-ratio card passes palette-conformance test: ≥ 80% pixels within ΔE-CIE2000 < 10 of brand colors or white/black (FR-11.2)
4. Dispatcher transitions to `done`; prints Review Checklist

**Postconditions:**
- `09_graphics/`: `1x1.png` (1080×1080), `16x9.png` (1920×1080), `9x16.png` (1080×1920), `4x5.png` (1080×1350), `og_card.png` (1200×630)
- `09_graphics/stat_{1x1,16x9,9x16,4x5}.png` (because `has_stat_card=true`)
- `09_graphics/code.png` (because `has_code_screenshot=true`)
- `09_graphics/carousel/slide_{01..06}.png` each 1080×1350
- All aspect-ratio cards pass palette-conformance

### Alternative Flows

- **UC-11-A1: `has_stat_card=false`** — `_render_stat()` is not called; no `stat_*.png` files created; verified by asserting file absence (FR-11.5)
- **UC-11-A2: `has_code_screenshot=false`** — `_render_code()` is not called; no `code.png` created (FR-11.7)
- **UC-11-A3: Both flags false** — only 4 aspect-ratio cards + OG card + 6 carousel slides are produced; minimum output set

### Error Flows

- **UC-11-E1: Gemini Imagen rate-limit during aspect card rendering** — `GeminiRateLimited` raised; stage fails; partial output files may exist; operator reruns (dispatcher handles the `--rerun` reset)
- **UC-11-E2: Palette conformance failure on a card** — test helper raises assertion error; `validate_outputs()` fails; stage transitions to `failed`

### Data Requirements

- **Input:** `04_plan/brief.json`, `03_brand/proposal.json`, `03_brand/logo.png`, `03_brand/style_sheet.png`, brand fonts
- **Output:** 4–12 PNG files in `09_graphics/` depending on flags; 6 carousel slides in `09_graphics/carousel/`
- **Side effects:** Gemini Imagen calls (up to 10+); local PIL and Pygments rendering (no external API for code screenshot)

---

### UC-12: Run Stage 10 — Copy Generation

**Actor:** Operator
**Preconditions:**
- `04_plan`, `01_pick`, `02_enrich`, `03_brand` are all `done` and approved
- `~/.claude/agents/social-copywriter.md` exists

**Trigger:** `shipcast copy <slug>`

### Primary Flow (Happy Path)

1. `s10_copy.check_inputs()` verifies all upstream stages done+approved
2. `s10_copy.run()` invokes `social-copywriter` sub-agent via `claude -p` (300 s timeout) with `04_plan/brief.json`, `01_pick/entry.json`, `02_enrich/context.json`, `03_brand/voice.md` as context (FR-12.1)
3. Sub-agent returns `CopyBundle` JSON: `twitter_thread`, `linkedin`, `blog` (FR-12.2)
4. `s10_copy` writes three Markdown files:
   - `10_copy/twitter_thread.md`: 3–8 numbered tweets, each ≤ 280 chars (FR-12.3)
   - `10_copy/linkedin.md`: 600–1200 words, valid CommonMark (FR-12.3)
   - `10_copy/blog.md`: 1200–2000 words, valid CommonMark (FR-12.3)
5. Each file's opening line matches the hook template chosen in `brief.hook_template_per_channel` for that channel (FR-12.4)
6. Twitter file uses Unicode mathematical bold; no raw `**bold**` Markdown (FR-12.5)
7. Dispatcher transitions to `done`; prints Review Checklist

**Postconditions:**
- Three Markdown files exist; length and format constraints satisfied
- Each file's first non-blank line is a substring of the rendered hook template

### Error Flows

- **UC-12-E1: Sub-agent times out** — `SubagentTimeout`; stage fails
- **UC-12-E2: Output fails length validation** — `validate_outputs()` raises with specific constraint violation; stage fails

### Data Requirements

- **Input:** `04_plan/brief.json`, `01_pick/entry.json`, `02_enrich/context.json`, `03_brand/voice.md`
- **Output:** `10_copy/twitter_thread.md`, `10_copy/linkedin.md`, `10_copy/blog.md`
- **Side effects:** one `claude -p` subprocess

---

### UC-13: Run Stage 11 — Package

**Actor:** Operator
**Preconditions:**
- All upstream stages 01–10 are `done` and approved

**Trigger:** `shipcast package <slug>`

### Primary Flow (Happy Path)

1. `s11_package.check_inputs()` verifies stages 01–10 done+approved
2. `s11_package.run()`:
   a. Assembles `11_package/release.zip` containing all output files from stages 01–10; entries sorted deterministically (FR-13.1, FR-13.3)
   b. Writes `11_package/README.md` with asset Markdown table and three fenced code blocks (FR-13.2)
   c. Calls `code-reviewer` sub-agent via `claude -p` (300 s timeout) for README link sanity check (FR-13.5)
3. Conditional files (`stat_*.png`, `code.png`) included in ZIP when present (FR-13.4)
4. Dispatcher transitions to `done`; prints Review Checklist

**Postconditions:**
- `release.zip` listing includes all required files
- `README.md` has ≥ 3 fenced code blocks and ≥ 9-row asset table
- Re-running on identical inputs produces byte-identical `release.zip` (FR-13.3)

### Error Flows

- **UC-13-E1: `code-reviewer` sub-agent times out** — stage fails; operator reruns

### Data Requirements

- **Input:** all stage output files
- **Output:** `11_package/release.zip`, `11_package/README.md`
- **Side effects:** one `claude -p` subprocess; `release.zip` assembled in-memory with sorted entries

---

## CLI Control Flows

---

### UC-14: Approve a Done Stage (Standard, No Edits)

**Actor:** Operator
**Preconditions:**
- Stage `<stage_id>` has `status=done`; `outputs_hash_at_done` is populated
- No output files have been edited since the stage ran

**Trigger:** `shipcast approve <slug> <stage_id>`

### Primary Flow (Happy Path)

1. Operator runs `shipcast approve getdeal--csv-export 01_pick`
2. CLI loads manifest; reads `stages.01_pick.outputs` and `outputs_hash_at_done`
3. CLI recomputes `compute_outputs_hash` over all output files (byte-content SHA-256) (FR-1.13)
4. Current hash equals `outputs_hash_at_done` — no manual edits
5. `Manifest.approve("01_pick")` sets `human_approved_at = utcnow()`; `manually_edited` remains `false` (FR-1.15)
6. Manifest saved atomically; CLI exits 0

**Postconditions:**
- `human_approved_at` is set; `manually_edited=false`; downstream stages unblocked

### Alternative Flows

- **UC-14-A1: Output files edited before approving** — see UC-15

### Error Flows

- **UC-14-E1: Stage is not `done`** — `Manifest.approve()` raises `CannotApproveNonDoneStage`; CLI exits with code 1; manifest unmodified (FR-1.16)

---

### UC-15: Approve After Hand-Editing Stage Outputs

**Actor:** Operator
**Preconditions:**
- Stage has `status=done`; operator has edited one or more output files externally

**Trigger:** `shipcast approve <slug> <stage_id>` after file edits

### Primary Flow (Happy Path)

1. Operator edits `03_brand/proposal.json` with correct palette (FR-3.9)
2. Operator runs `shipcast approve getdeal--csv-export 03_brand`
3. CLI recomputes `compute_outputs_hash`; value differs from `outputs_hash_at_done`
4. CLI prints "Manual edits detected on N files" and lists each changed file (FR-1.15)
5. `Manifest.approve()` sets `human_approved_at = utcnow()`, `manually_edited = true` (FR-1.15)
6. Manifest saved atomically; CLI exits 0

**Postconditions:**
- `manually_edited=true`; `human_approved_at` set; downstream unblocked

### Edge Cases

- **UC-15-EC1:** Operator replaces a file with byte-identical content (same bytes, different mtime) — `compute_outputs_hash` uses byte-content SHA-256 (not mtime); hashes are equal; `manually_edited` remains `false` (FR-1.13)
- **UC-15-EC2:** Multiple files changed — all are listed; single `manually_edited=true` recorded

---

### UC-16: Re-run a Stage to Regenerate Output

**Actor:** Operator
**Preconditions:**
- Stage `<stage_id>` has `status=done`
- Operator wants to regenerate (e.g., get a different LLM output or update after input changes)

**Trigger:** `shipcast <verb> <slug> --rerun`

### Primary Flow (Happy Path)

1. Operator runs `shipcast script getdeal--csv-export --rerun`
2. Dispatcher reads `stages.05_script.status = "done"` (FR-1.17)
3. Dispatcher calls `Manifest.reset("05_script")` → transitions `done → pending`; clears all fields; resets downstream stages `06_video_assets` through `11_package` transitively
4. Manifest saved atomically
5. Dispatcher proceeds with normal stage-run flow (acquires lock, `check_inputs`, `run`, `validate_outputs`, transitions to `done`)
6. Review Checklist printed with updated artifact paths; `human_approved_at = null` (operator must re-approve)

**Postconditions:**
- Stage is `done` again with new `outputs_hash_at_done`; `human_approved_at = null`
- All downstream stages are `pending`

### Alternative Flows

- **UC-16-A1: `--rerun` on `pending` or `failed` stage** — `--rerun` is a no-op; informational log line printed; stage runs normally from current state (FR-1.17)

### Error Flows

- **UC-16-E1: `--rerun` on `running` stage** — `StageBusy` raised; manifest unmodified; CLI exits non-zero (FR-1.17)
- **UC-16-E2: Stage has downstream approvals** — see UC-17 (cascade-confirmation guard)

---

### UC-17: Reset or Re-run with Downstream Approvals — Cascade-Confirmation Guard

**Actor:** Operator
**Preconditions:**
- Stage `<stage_id>` has `status=done`
- At least one downstream stage has `human_approved_at` non-null

**Trigger:** `shipcast <verb> <slug> --rerun` or `shipcast reset <slug> <stage_id>` (without `--yes`)

### Primary Flow (Happy Path — Operator Confirms)

1. Operator runs `shipcast brand getdeal--csv-export --rerun` when `04_plan` is approved
2. Dispatcher detects that downstream stage `04_plan` has `human_approved_at` non-null (FR-1.19)
3. Dispatcher prints: "WARNING: This will discard the following approvals: [04_plan, 05_script, ...]"
4. Without `--yes`, dispatcher prompts for confirmation (FR-1.19)
5. Operator types `y` to confirm; dispatcher proceeds with reset and re-run (UC-16 flow)
6. All listed downstream `human_approved_at` values are cleared as part of transitive reset

### Alternative Flows

- **UC-17-A1: `--yes` flag provided** — confirmation prompt is skipped; cascade proceeds immediately (FR-1.19)

### Error Flows

- **UC-17-E1: Operator types anything other than `y`** — cascade is aborted; no manifest modification; CLI exits 0

---

### UC-18: Reset a Stage

**Actor:** Operator
**Preconditions:**
- Stage has been run at least once (status is `done`, `failed`, or `needs_review`)
- Operator wants to discard outputs and restart

**Trigger:** `shipcast reset <slug> <stage_id> --yes`

### Primary Flow (Happy Path)

1. Operator runs `shipcast reset getdeal--csv-export 05_script --yes` (FR-1.18)
2. `--yes` bypasses confirmation prompt
3. CLI reads `stages.05_script.outputs` list; deletes each output file from disk (FR-1.18)
4. `Manifest.reset("05_script")` clears all fields (`status=pending`, `outputs=[]`, clears all hashes, timestamps, error); resets all downstream stages transitively (FR-1.18)
5. Manifest saved atomically; CLI exits 0

**Postconditions:**
- `05_script` and all downstream stages have `status=pending`; no output files remain

### Alternative Flows

- **UC-18-A1: No `--yes` flag** — interactive confirmation prompt; operator types `y` to confirm, any other key to abort
- **UC-18-A2: Stage is already `pending`** — reset is effectively a no-op (already clear); CLI completes successfully
- **UC-18-A3: Output file missing on disk** — CLI logs warning but continues; manifest is still updated

---

### UC-19: View Project Status

**Actor:** Operator
**Preconditions:**
- `projects/<slug>/manifest.json` exists with `schema_version=1`

**Trigger:** `shipcast status <slug>`

### Primary Flow (Happy Path)

1. Operator runs `shipcast status getdeal--csv-export`
2. CLI loads manifest; reads all eleven stage records (FR-1.2)
3. CLI renders a `rich.Table` with eleven rows; each row: stage id, status (color-coded: pending=grey, running=yellow, done=green, failed=red, needs_review=cyan), approval indicator (FR-1.2)
4. CLI exits 0; manifest unmodified; no lock acquired

**Postconditions:**
- Colorized 11-row table printed to stdout

---

### UC-20: Cost Cap Enforcement

**Actor:** Dispatcher (automated, triggered during stage dispatch)
**Preconditions:**
- `CostLedger` accumulated cost is approaching `Settings.max_cost_usd_per_project`

**Trigger:** Dispatcher attempts to invoke a paid API call (Veo, Gemini Imagen, Gemini multimodal, ElevenLabs)

### Primary Flow (Happy Path — Cap Would Be Exceeded)

1. Dispatcher checks accumulated `manifest.stages[*].metrics.cost_usd` total before invoking any paid call (FR-1.28)
2. Next call's unit cost would push total above `max_cost_usd_per_project` ($3 standard / $8 premium)
3. Dispatcher raises `CostCapExceeded`; transitions the current stage to `failed`; saves manifest; exits non-zero (FR-1.28)
4. No external API call is made

**Postconditions:**
- Stage transitions to `failed` with `error.type = "CostCapExceeded"`
- Accumulated cost does not increase

### Alternative Flows

- **UC-20-A1: Cost is within cap** — dispatcher proceeds; paid API call is made; `metrics.cost_usd` updated in manifest after call completes

---

### UC-21: Concurrent Invocation Lock Contention

**Actor:** Two Operator processes running simultaneously
**Preconditions:**
- `projects/<slug>/` exists; no process holds `.lock`

**Trigger:** Two `shipcast` commands for the same slug launched nearly simultaneously

### Primary Flow (Happy Path)

1. Process A acquires `fcntl.flock(LOCK_EX|LOCK_NB)` on `projects/<slug>/.lock` (FR-1.30)
2. Process B attempts the same lock; `BlockingIOError` raised; dispatcher raises `ProjectLocked`; exits with code 2 (FR-1.30)
3. Process A completes its stage run normally; releases lock

**Postconditions:**
- Exactly one process exits 0; exactly one exits 2 with `ProjectLocked`

---

### UC-22: `--no-lock` Requiring `SHIPCAST_NO_LOCK_ACK=1`

**Actor:** Operator
**Preconditions:**
- Operator wants to bypass locking (e.g., CI context)

**Trigger:** `shipcast pick <slug> --no-lock`

### Primary Flow (Happy Path — Ack Set)

1. Operator runs `SHIPCAST_NO_LOCK_ACK=1 shipcast pick getdeal--csv-export --no-lock` (FR-1.32)
2. CLI detects `--no-lock` flag; checks `SHIPCAST_NO_LOCK_ACK=1` is set
3. Dispatcher prints yellow warning banner (FR-1.32)
4. Lock acquisition step is skipped; stage runs normally (UC-2 flow)
5. CLI exits 0

### Error Flows

- **UC-22-E1: `--no-lock` without `SHIPCAST_NO_LOCK_ACK=1`** — dispatcher raises `LockBypassNotAcknowledged`; exits non-zero; no stage execution (FR-1.32)

---

### UC-23: Unsupported Platform

**Actor:** Operator on Windows or other non-POSIX OS
**Preconditions:**
- `shipcast` installed on Windows (or any non-macOS/non-Linux platform)

**Trigger:** Any `shipcast` CLI invocation

### Primary Flow

1. Any `shipcast` command is run on Windows
2. At CLI startup, before any operation, platform check fires (FR-1.31)
3. `UnsupportedPlatform` raised; clear error message printed
4. CLI exits non-zero immediately; no files created or modified

---

## Error Flows

---

### UC-24: Upstream Stage Not Done — Downstream Refuses

**Actor:** Operator
**Preconditions:**
- `01_pick` has `status=pending` (never run)

**Trigger:** `shipcast enrich <slug>` when `01_pick` is not done

### Primary Flow

1. Dispatcher acquires lock; transitions `02_enrich` to `running`
2. `s02_enrich.check_inputs()` finds `stages.01_pick.status = "pending"` (not `done`) (FR-1.24)
3. `StageInputMissing` raised; stage transitions to `failed`; `error.type = "StageInputMissing"`; lock released; exits non-zero (code 2)

**Postconditions:**
- `02_enrich.status = "failed"`; `01_pick` unchanged

---

### UC-25: Upstream Stage Done but Not Approved

**Actor:** Operator
**Preconditions:**
- `01_pick` has `status=done` but `human_approved_at = null`

**Trigger:** `shipcast enrich <slug>`

### Primary Flow

1. `s02_enrich.check_inputs()` finds `01_pick.status=done` but `human_approved_at=null` (FR-1.14)
2. `StageNotApproved` raised; stage transitions to `failed`; exits with code 2 (FR-1.14)

---

### UC-26: Manifest Schema Version Mismatch

**Actor:** Operator
**Preconditions:**
- `manifest.json` has `schema_version` other than `1`

**Trigger:** Any command calling `Project.load(<slug>)`

### Primary Flow

1. `Manifest.load()` reads `schema_version != 1`; raises `ManifestMigrationNeeded` (FR-1.12)
2. CLI prints clear error with migration guidance; exits non-zero; manifest unmodified

---

### UC-27: `config_snapshot` Mutation Attempt After Stage Runs

**Actor:** Code calling `Manifest.update_config_snapshot()`
**Preconditions:**
- At least one stage has left `pending` status

**Trigger:** Code calls `Manifest.update_config_snapshot(new_settings)`

### Primary Flow

1. `update_config_snapshot()` checks: any stage not `pending`? Yes
2. Raises `ConfigSnapshotLocked` (FR-1.11)
3. `config_snapshot` in manifest is NOT modified; stage statuses unchanged

### Edge Cases

- **UC-27-EC1:** `s03_brand` runs and outputs brand files — brand data lives in `03_brand/` files; nothing is written to `config_snapshot`; this path must never reach `update_config_snapshot` (FR-3.11, FR-1.11)

---

### UC-28: Sub-agent Error Paths

**Actor:** Dispatcher (during stage execution)
**Preconditions:**
- A stage invokes a `claude -p` sub-agent subprocess

**Trigger:** Sub-agent invocation during `s02_enrich`, `s04_plan`, `s05_script`, `s10_copy`, or `s11_package`

### Primary Flow (Timeout)

1. Sub-agent subprocess does not return within 300 s (FR-5.3)
2. Stage raises `SubagentTimeout`; transitions to `failed`; `error.type = "SubagentTimeout"`, `error.log_path` populated
3. CLI exits non-zero; operator reruns after resolving the cause

### Alternative Flows

- **UC-28-A1: Sub-agent exits non-zero** — stage fails; stderr captured in `error.message`
- **UC-28-A2: Sub-agent stdout is not valid JSON** — stage raises `SubagentMalformedOutput`; stage fails

### Data Requirements

- **Side effects:** partial artifacts may exist on disk; operator uses `--rerun` or `reset` to restart

---

### UC-29: Manifest Atomic Write — Simulated Mid-Write Crash

**Actor:** System (fault injection)
**Preconditions:**
- `os.replace` is monkeypatched to raise after `manifest.json.tmp` is written

**Trigger:** Any manifest save operation

### Primary Flow

1. `Manifest.save()` serializes content to `manifest.json.tmp` with `fsync` (FR-1.9)
2. Monkeypatched `os.replace` raises `OSError` before rename completes
3. `manifest.json` remains unmodified (original content intact) (FR-1.9)
4. `manifest.json.tmp` exists on disk with the new content

**Postconditions:**
- `manifest.json` contains pre-crash content; no state corruption (NFR-16.7)

---

## Edge Cases

---

### UC-30: Empty CHANGELOG Produces Empty List

**Actor:** Operator
**Preconditions:**
- Target repo's `CHANGELOG.md` exists but is empty

**Trigger:** `shipcast pick <slug>`

### Primary Flow

1. `changelog/parser.py` reads the empty file; returns `[]` (FR-4.2)
2. `s01_pick.run()` finds no entries to match against; raises `ChangelogEntryNotFound`
3. Stage transitions to `failed`; CLI exits non-zero

---

### UC-31: Idempotent Re-run of Deterministic Stages

**Actor:** Operator
**Preconditions:**
- A deterministic stage (`s01_pick`, `s11_package`) has run and produced output
- Same input files, no changes

**Trigger:** Stage re-run on identical inputs

### Primary Flow

1. Stage runs on identical inputs
2. Output artifact bytes are identical to the first run (FR-4.5, FR-13.3, NFR-16.6)
3. `compute_outputs_hash` produces the same value as the original `outputs_hash_at_done`

**Postconditions:**
- `manually_edited` would remain `false` if operator approves immediately (no spurious hash mismatch)

---

### UC-32: Attempting to Run Stage with `status=running` (Stale Lock After Crash)

**Actor:** Operator
**Preconditions:**
- A previous run crashed mid-execution; stage has `status=running`; `.lock` is no longer held (crash released advisory lock)

**Trigger:** `shipcast <verb> <slug>` (without `--rerun`)

### Primary Flow

1. Dispatcher acquires lock (succeeds; advisory lock was released by kernel on crash)
2. Dispatcher reads `stages.<id>.status = "running"`; no `--rerun` flag
3. Stage transitions `running → running` — this is an `IllegalTransition` (FR-1.8)
4. Operator must use `--rerun` to reset from `running` state; or `shipcast reset`

### Alternative Flows

- **UC-32-A1: `--rerun` passed** — `StageBusy` check fires if status is truly `running` (concurrent live process); but if the lock is acquirable, it implies no live process and `--rerun` proceeds

---

### UC-33: Palette Hint Skips Playwright Extract

**Actor:** Operator
**Preconditions:**
- `projects/_brand/<brand_slug>/palette.hint.json` exists with `{"primary": "#...", "accent": "#...", "neutral": "#..."}`

**Trigger:** `shipcast brand <slug>`

### Primary Flow

1. `s03_brand.check_inputs()` detects `palette.hint.json` in brand pack
2. `run()` reads the three hex values from hint; skips `playwright_client.extract_css_palette()` and `extract_font_family()` entirely (FR-3.2)
3. `proposal.json` is written with the three hint values; no Playwright browser launched for palette extraction

**Postconditions:**
- No Playwright call for palette; stage completes faster; SSRF surface reduced
- `playwright_client.extract_css_palette` is never called (assertable in tests via mock)

---

### UC-34: Cost Ledger — Per-Stage Accumulation and Mode-Dependent Cap

**Actor:** Dispatcher
**Preconditions:**
- `input.yaml.video_mode` is set (standard or premium)

**Trigger:** Each paid API call during pipeline execution

### Primary Flow

1. Before each paid API call, dispatcher reads accumulated `cost_usd` from all stage records (FR-1.28)
2. Adds the unit cost for the next call (from `cost.py` constants: Veo=$3.20, Imagen=$0.04, multimodal=$0.01, ElevenLabs=$0.30/min) (FR-1.27)
3. If sum ≤ `max_cost_usd_per_project`, call proceeds; `metrics.cost_usd` updated in manifest after call
4. Standard mode: total ≤ $3.00; premium mode: total ≤ $8.00 (FR-1.28, FR-15.2)

### Edge Cases

- **UC-34-EC1:** Accumulated cost exactly equals cap — next call would exceed; `CostCapExceeded` raised; no rounding errors (check is `projected_total > cap`, not `≥`)

---

### UC-35: `shipcast --help` and Startup Latency

**Actor:** Operator
**Preconditions:**
- `shipcast` is installed

**Trigger:** `shipcast --help`

### Primary Flow

1. `shipcast --help` exits 0 and lists all eleven verb names (FR-1.3)
2. Completes in under 1 second (NFR-16.10)
3. No external API client is instantiated at startup (FR-1.3, clients are lazy)

---

### UC-36: Missing API Key — Lazy Client Construction

**Actor:** Dispatcher during stage execution
**Preconditions:**
- A required API key (e.g., `GEMINI_API_KEY`) is empty or absent from `.env`
- The stage needing that key is being dispatched

**Trigger:** `shipcast brand <slug>` (or any stage requiring a client) with missing key

### Primary Flow

1. Dispatcher runs `s03_brand.run()`; inside `run()`, `GeminiClient(api_key=settings.gemini_api_key)` is instantiated lazily
2. `GeminiClient.__init__` detects empty `SecretStr`; raises `MissingApiKey("GEMINI_API_KEY")` — key NAME only, never the (empty) value (FR-1.29)
3. Stage transitions to `failed`; `error.type = "MissingApiKey"`, `error.message = "GEMINI_API_KEY"`
4. CLI exits non-zero

**Postconditions:**
- Stage is `failed`; no API key value is logged anywhere

---

## Coverage Matrix

This table maps every PRD section and key FR/AC to the use cases that exercise it.

| PRD Section / FR | Description | Covered by UC(s) |
|---|---|---|
| **Section 1 — Factory Scaffold** | | |
| FR-1.1 | `shipcast pick` creates project, writes manifest, all stages pending | UC-1, UC-2 |
| FR-1.2 | `shipcast status` renders 11-row color-coded table | UC-19 |
| FR-1.3 | `shipcast --help` exits 0, lists 11 verbs; ≤1 s startup | UC-35 |
| FR-1.4 | Project directory layout with manifest as single source of truth | UC-1, UC-2 |
| FR-1.7 | `StageRecord` fields including `metrics.cost_usd` | UC-2, UC-34 |
| FR-1.8 | `Manifest.transition` legal transitions only | UC-2, UC-32 |
| FR-1.9 | Atomic manifest write (tmp+fsync+os.replace) | UC-29 |
| FR-1.10 | Deterministic JSON serialization | UC-31 |
| FR-1.11 | `config_snapshot` locked after first stage leaves pending | UC-27 |
| FR-1.12 | `schema_version != 1` raises `ManifestMigrationNeeded` | UC-26 |
| FR-1.13 | Two-hash-family asymmetry (inputs=mtime, outputs=bytes) | UC-14, UC-15, UC-31 |
| FR-1.14 | Human-approval gate; downstream refuses if upstream not approved | UC-14, UC-25 |
| FR-1.15 | `approve` recomputes hash; `manually_edited=true` on mismatch | UC-15 |
| FR-1.16 | `CannotApproveNonDoneStage` (exit 1) | UC-14-E1 |
| FR-1.17 | `--rerun` semantics: done→reset+run; pending/failed→no-op; running→StageBusy | UC-16 |
| FR-1.18 | `reset` deletes outputs, resets stage + downstream transitively | UC-18 |
| FR-1.19 | Cascade-confirmation guard for reset/rerun with downstream approvals | UC-17 |
| FR-1.20 | Dispatcher owns full execution wrapper | UC-2 |
| FR-1.21 | `check_inputs` verifies upstream done+approved+files+hash | UC-24, UC-25 |
| FR-1.22 | Review Checklist printed after every successful stage | UC-2, UC-6 |
| FR-1.23 | `Stage` protocol contract | UC-2, UC-6 |
| FR-1.24 | `BaseStage.check_inputs` behavior | UC-24, UC-25, UC-2-E3 |
| FR-1.27 | Cost ledger constants; per-stage `metrics.cost_usd` | UC-34 |
| FR-1.28 | `CostCapExceeded` check before paid API call | UC-20, UC-34 |
| FR-1.29 | `config_snapshot` excludes `SecretStr` fields; `MissingApiKey` in client | UC-36 |
| FR-1.30 | `fcntl.flock`; `ProjectLocked` on contention | UC-21 |
| FR-1.31 | `UnsupportedPlatform` on non-macOS/Linux | UC-23 |
| FR-1.32 | `--no-lock` requires `SHIPCAST_NO_LOCK_ACK=1` | UC-22 |
| FR-1.33 | Logging setup: RichHandler + JSON-line file handler | UC-2 |
| **Section 2 — Input Validation** | | |
| FR-2.1 | `InputYaml` Pydantic model fields | UC-2 |
| FR-2.3 | `live_url` validators: https-only, rejects RFC1918/loopback/link-local | UC-2-E4, UC-4-E5 |
| FR-2.4 | `repo_path` validators: no `..`, under allowed root, has CHANGELOG.md | UC-2-E5 |
| FR-2.5, FR-2.6 | `WalkthroughStep` validator (rejects `javascript:`, unknown actions) | UC-2-E4 |
| FR-2.7 | Validation errors transition stage to `failed` with field list | UC-2-E4, UC-2-E5 |
| **Section 3 — Brand Pack / s03_brand** | | |
| FR-3.1, FR-3.2 | Brand pack required/optional files | UC-4, UC-4-E1, UC-4-E2, UC-4-E3 |
| FR-3.3 | `BrandPackIncomplete` raised before any API call | UC-4-E1, UC-4-E2, UC-4-E3 |
| FR-3.4 | Playwright methods: palette, font, logo | UC-4 |
| FR-3.5 | `PlaywrightTimeout` (60 s nav limit) | UC-4-E4 |
| FR-3.6 | URL validator called before Playwright | UC-4-E5 |
| FR-3.7 | Gemini `generate_image(aspect_ratio="1:1")` for style sheet | UC-4 |
| FR-3.8 | Three outputs written: `proposal.json`, `logo.png`, `style_sheet.png` | UC-4 |
| FR-3.9 | Operator edits proposal.json between run and approve | UC-4, UC-15 |
| FR-3.10 | `manually_edited=true` on hash mismatch at approve | UC-15 |
| FR-3.11 | Brand data never in `config_snapshot` | UC-4, UC-27-EC1 |
| FR-3.12 | `gemini_client.generate_image(aspect_ratio=...)` parameter | UC-4, UC-11 |
| **Section 4 — s01_pick** | | |
| FR-4.1, FR-4.2 | Changelog parser; `ChangelogFileMissing` on missing file | UC-2, UC-2-E1 |
| FR-4.3 | `ChangelogEntryNotFound` on heading mismatch | UC-2-E2 |
| FR-4.4 | `entry.json` deterministic serialization | UC-2 |
| FR-4.5 | Idempotent: same input → byte-identical output | UC-2-A3, UC-31 |
| **Section 5 — s02_enrich** | | |
| FR-5.1 | Three sub-steps: gh/git, Playwright walkthrough, Gemini multimodal | UC-3 |
| FR-5.2 | `live_url` absent → Playwright skipped; `screenshots: []` | UC-3-A1 |
| FR-5.3 | `ba-analyst` sub-agent; `SubagentTimeout`, `SubagentMalformedOutput` | UC-3-E1, UC-3-E2, UC-3-E3, UC-28 |
| FR-5.4 | `context.json` validates against `EnrichedContext` | UC-3 |
| FR-5.5 | `gemini_client.multimodal()` method | UC-3 |
| **Section 6 — s04_plan** | | |
| FR-6.1, FR-6.2 | Chained `planner` + `brand-guardian` sub-agents | UC-5 |
| FR-6.3 | Sub-agent error classes | UC-5-E1, UC-5-E2, UC-5-E3, UC-28 |
| FR-6.4 | `MarketingBrief` schema constraints | UC-5 |
| FR-6.5 | 7-template hook catalog in `hooks.py` | UC-5, UC-12 |
| FR-6.6 | `brief.json` output | UC-5 |
| **Section 7 — s05_script** | | |
| FR-7.1–FR-7.4 | `demo-script-writer` sub-agent; storyboard 4–6 beats | UC-6 |
| **Section 8 — s06_video_assets** | | |
| FR-8.1, FR-8.2 | Standard mode: Imagen still + Ken-Burns per beat | UC-7 |
| FR-8.3, FR-8.4 | Premium mode: Veo 3 Fast for beat[0]; Imagen+KB for beats[1..3] | UC-8 |
| FR-8.5 | `VeoSafetyBlocked` → silent per-beat fallback; prompt not logged | UC-8-E1 |
| FR-8.6 | `VeoQuotaExceeded` → stage fails immediately | UC-8-E2 |
| FR-8.7 | Clips validated via `ffprobe` | UC-7, UC-8 |
| FR-8.8 | `--no-veo` flag forces Imagen+KB for all beats | UC-8-A1 |
| **Section 9 — s07_voice** | | |
| FR-9.1–FR-9.4 | ElevenLabs synthesis + WhisperX timestamps | UC-9 |
| FR-9.5 | `ElevenLabsQuotaExceeded` on 429 | UC-9-E1 |
| **Section 10 — s08_video** | | |
| FR-10.1 | `_assemble_raw()`: concat + narration + optional bgm ducking | UC-10, UC-10-A1 |
| FR-10.2 | `_overlay_captions()`: reads `caption_mode` from `voice.md`; default `chip` | UC-10-A2, UC-10-A3, UC-10-A4 |
| FR-10.4 | `_export_loop()`: 6 s, center-crop 1080×1080, no audio, mp4+gif | UC-10 |
| FR-10.5 | Three output files written | UC-10 |
| **Section 11 — s09_graphics** | | |
| FR-11.1, FR-11.2 | 4 aspect-ratio cards via Gemini + PIL + palette conformance | UC-11 |
| FR-11.3 | OG card 1200×630 | UC-11 |
| FR-11.4, FR-11.5 | Stat card conditional on `has_stat_card` | UC-11, UC-11-A1 |
| FR-11.6, FR-11.7 | Code screenshot conditional on `has_code_screenshot` | UC-11, UC-11-A2 |
| FR-11.8, FR-11.9 | Carousel: 6 slides at 1080×1350 | UC-11 |
| **Section 12 — s10_copy** | | |
| FR-12.1–FR-12.5 | `social-copywriter` sub-agent; 3 Markdown files; hook-template opening | UC-12 |
| **Section 13 — s11_package** | | |
| FR-13.1–FR-13.5 | `release.zip` + README; `code-reviewer` sub-agent; idempotent zip | UC-13 |
| **Section 14 — Marketing Constraints** | | |
| FR-14.1–FR-14.8 | Typography, color, palette conformance, spacing, motion, caption modes | UC-4, UC-10, UC-10-A2, UC-11 |
| **Section 15 — Two Video Modes** | | |
| FR-15.1 | `video_mode` accepts only `"standard"` or `"premium"` | UC-2, UC-7, UC-8 |
| FR-15.2 | Mode-dependent cost caps ($3/$8) | UC-20, UC-34 |
| FR-15.3 | Full Veo 3 not supported; raises `UnsupportedVideoMode` | (validation path in UC-2) |
| FR-15.4 | `--no-veo` flag | UC-8-A1 |
| **Section 16 — NFRs** | | |
| NFR-16.3 | macOS/Linux only | UC-23 |
| NFR-16.4 | Cost caps enforced at runtime | UC-20 |
| NFR-16.6 | Idempotency of deterministic stages | UC-31 |
| NFR-16.7 | Atomic manifest writes | UC-29 |
| NFR-16.8 | Security: no secrets in logs or `config_snapshot` | UC-36, UC-27 |
| NFR-16.10 | `--help` completes in < 1 s | UC-35 |

---

## Gap Analysis

**FRs with no dedicated UC:** FR-1.25 (`additional_input_paths` hook — exercised implicitly within UC-4), FR-1.26 (`pre_run_hook` test seam — test-only, no behavioral UC needed). All behavioral FRs are covered.

**ACs with no dedicated UC:** AC-1.2 (`mypy --strict`), AC-1.3 (`ruff check`), AC-1.4 (coverage gates), AC-12.1, AC-12.2, AC-12.3 (format assertions within UC-12), AC-14.1 (catalog unit test within UC-5) — these are static-analysis or fine-grained assertion gates that are covered within the containing stage UCs rather than as standalone flows.

**Conclusion:** All 101 FRs across 17 PRD sections are covered by at least one use case. All error classes named in the PRD and reference plan are assigned to a specific UC error flow.

---

## Summary Counts

| Category | Count |
|---|---|
| Primary flows (happy paths, per-stage + CLI) | 23 |
| Alternative flows | 22 |
| Error flows | 34 |
| Edge cases | 14 |
| **Total use cases (UC-1 through UC-36)** | **36** |
