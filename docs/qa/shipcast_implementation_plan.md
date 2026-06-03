# Implementation Plan: shipcast Auto-Marketing Pipeline

> Tech Lead binding slice-by-slice plan. Executed one slice per `/implement-slice` (tests-first TDD → implement → verify → atomic commit).
> Sources: `docs/PRD.md` (17 sections, 101 FRs), `docs/use-cases/shipcast_use_cases.md` (36 UCs), `docs/qa/shipcast_test_cases.md` (112 TCs), `docs/architecture/shipcast_architecture_review.md` (PASS-with-conditions).

## Prerequisites verified

- PRD: `docs/PRD.md` — Sections 1–17, 101 FRs (all "Defined"). ✓
- Use cases: `docs/use-cases/shipcast_use_cases.md` — 36 UCs (UC-1..UC-36), 93 sub-flows. ✓
- QA test cases: `docs/qa/shipcast_test_cases.md` — 112 TCs (TC-1.1..TC-23.4). ✓
- Architecture review: **PASS (with conditions)** — 3 MAJOR + 4 MINOR findings, all assigned to owning slices below.

## Stage IDs (FIXED — do not renumber)

`01_pick · 02_enrich · 03_brand · 04_plan · 05_script · 06_video_assets · 07_voice · 08_video · 09_graphics · 10_copy · 11_package`

## Sequencing model

All 24 slices are **sequential-within-wave**. Every slice that adds a stage edits the registry tuple `src/shipcast/stages/__init__.py::ALL_STAGES` and the verb registry in `src/shipcast/cli.py` (confirmed pattern in the upstream scaffold`s `stages/__init__.py` lines 8–34 and `cli.py`). Because every wave's slices share those two files, parallelization within a wave would conflict; sequential is the correct and safe model. Waves order the dependency chain; the registry edit is the unavoidable shared file. Each slice = one atomic commit (`feat(core)` for any `src/shipcast/` change, else `feat(infra)`/`chore(infra)`), on a `feat/shipcast-vN` branch — never on `main`.

---

## Wave 1 — Scaffold + Core Invariants

### Slice 1 — Project scaffold, manifest, CLI dispatcher, locking (carried over verbatim/renamed from the upstream scaffold)
- **Wave:** 1
- **Covers:** UC-1, UC-2, UC-14, UC-15, UC-16, UC-17, UC-18, UC-19, UC-21, UC-22, UC-23, UC-24, UC-25, UC-26, UC-27, UC-29, UC-32, UC-35, UC-36 · **TCs:** TC-1.1, TC-1.2, TC-1.3, TC-1.10, TC-1.11, TC-1.12, TC-1.13, TC-1.14, TC-1.15, TC-1.16, TC-1.17, TC-2.1, TC-2.2, TC-2.3, TC-2.4, TC-2.5, TC-2.6, TC-15.1–TC-15.6, TC-16.1–TC-16.9, TC-18.1–TC-18.4, TC-19.1, TC-19.3, TC-19.4, TC-19.5, TC-23.3, TC-23.4
- **Files:**
  - Carried-over-verbatim-from-the-upstream-scaffold (package rename to `shipcast`): `src/shipcast/manifest.py`, `src/shipcast/stage.py`, `src/shipcast/stages/_base.py` [extend `__init__.py`], `src/shipcast/locking.py`, `src/shipcast/errors.py`, `src/shipcast/paths.py`, `src/shipcast/logging_setup.py`, `src/shipcast/audio.py`, `src/shipcast/prompts.py`
  - Copied-with-rename: `src/shipcast/cli.py` (verb registry → 11 shipcast verbs; `FACTORY_NO_LOCK_ACK`→`SHIPCAST_NO_LOCK_ACK`), `src/shipcast/project.py`, `src/shipcast/__main__.py` [new]
  - Verbatim clients (so lazy `__getattr__` registry resolves): `src/shipcast/clients/__init__.py` [extend], `src/shipcast/clients/anthropic_client.py`, `src/shipcast/clients/elevenlabs_client.py`, `src/shipcast/clients/whisperx_client.py`, `src/shipcast/clients/ffmpeg_client.py`
  - `projects/_template/` (already on disk — 11 stage dirs + `input.yaml`; verified)
  - Tests [new]: `tests/conftest.py`, `tests/unit/test_manifest.py`, `tests/unit/test_cli_dispatch.py`, `tests/unit/test_locking.py`, `tests/unit/test_logging.py`, `tests/unit/test_package_imports.py`, `tests/integration/test_human_gate.py`, `tests/integration/test_rerun_reset.py`
- **Changes:** Rename the upstream package to `shipcast` across copied modules; replace the verb registry tuple in `cli.py` with the 11 shipcast verbs (`pick, enrich, brand, plan, script, video_assets, voice, video, graphics, copy, package`) and `status`/`approve`/`reset`; keep `ALL_STAGES=()` empty for now (stages land in later slices), so `--help` lists verbs but no stage runs yet. **Note (TC-19.3 discrepancy):** the reused `AnthropicClient` uses the `claude` CLI subscription and takes NO api key (the upstream scaffold`s `anthropic_client.py` lines 8–11, 28–35). TC-19.3 asserts `MissingApiKey` for Gemini/ElevenLabs/Anthropic; restrict the Anthropic assertion to "no api-key constructor" (document the subscription model) and keep `MissingApiKey("GEMINI_API_KEY"|"ELEVENLABS_API_KEY")` for the two real-key clients — classify as error-recovery Rule 1 during implementation.
- **Verify:** `uv run shipcast --help && uv run mypy --strict src/shipcast && uv run ruff check src tests && uv run pytest -v tests/unit/test_manifest.py tests/unit/test_cli_dispatch.py tests/unit/test_package_imports.py`
- **Done when:** `shipcast --help` exits 0 in <1 s and prints all 11 verb names (TC-2.1); the 8 legal transitions pass and every illegal pair raises `IllegalTransition` (TC-1.1/TC-1.2); monkeypatched `os.replace` leaves `manifest.json` byte-unchanged with a `.tmp` present (TC-1.3); `--no-lock` without `SHIPCAST_NO_LOCK_ACK=1` raises `LockBypassNotAcknowledged` (TC-18.2); two-process Popen race yields exactly one exit-0 and one exit-2 `ProjectLocked` (TC-18.1); `Windows` platform monkeypatch raises `UnsupportedPlatform` before any FS op (TC-2.5); `.env.example` scan finds only `KEY=` lines (TC-19.1).
- **Pre-review:** none
- **Est. LoC:** mostly mechanical rename of existing upstream-scaffold code (~0 net new logic) + ~120 LoC test-only. Under 200. ✓

### Slice 2 — Cost ledger + Settings + config_snapshot secret-exclusion *(SECURITY PRE-REVIEW)*
- **Wave:** 2
- **Covers:** UC-20, UC-34, UC-36 · **TCs:** TC-17.1, TC-17.2, TC-17.5, TC-17.6, TC-19.2
- **Files:** `src/shipcast/cost.py` [new — `CostLedger`, per-tool unit-cost constants], `src/shipcast/config.py` [copied-with-rename from the upstream scaffold; add `max_cost_usd_per_project` derived from `video_mode`, `voice_id`; `public_dict()` SecretStr-exclusion verbatim], `src/shipcast/cli.py` [extend — dispatcher pre-call cap gate], `config.toml` [edit — remove stale `anthropic_model="claude-opus-4-7"` per Finding 4, since the `claude -p` subprocess path picks its own model from agent frontmatter], `tests/unit/test_cost_ledger.py` [new], `tests/unit/test_settings_secrets.py` [new]
- **Changes:** `CostLedger` reads accumulated `manifest.stages[*].metrics.cost_usd`; dispatcher computes `projected = accumulated + next_unit_cost` and raises `CostCapExceeded` when `projected > cap` (strict `>`). Unit costs from `config.toml [cost]`: Veo Fast $3.20, Imagen $0.04, multimodal $0.01, ElevenLabs $0.30/min. On `--rerun`/reset the reset stage's `metrics.cost_usd` is cleared, so re-accumulation starts from surviving stages' totals only (no double-count). **Finding 4 (MINOR):** delete `[models].anthropic_model` from `config.toml`.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/unit/test_cost_ledger.py tests/unit/test_settings_secrets.py`
- **Done when:** accumulated $2.97 + $0.04 Imagen → `CostCapExceeded`, client mock never called (TC-17.1); accumulated exactly $3.00 + $0.04 → `CostCapExceeded` (projected $3.04 > $3.00) (TC-17.5); accumulated $0.50 + $0.04 proceeds and updates `metrics.cost_usd` after the call (TC-17.2); reset-then-rerun starts that stage's cost from $0 with no double-count (TC-17.6); `settings.public_dict()` contains none of the three key names or their values (TC-19.2).
- **Pre-review:** **security** — verify cap check is a true pre-condition (projected total before the call), `metrics.cost_usd` monotonic and cannot underflow, no double-count on rerun/reset.
- **Est. LoC:** ~110. ✓

### Slice 3 — InputYaml + WalkthroughStep schema + URL/path validators *(SECURITY PRE-REVIEW)*
- **Wave:** 3
- **Covers:** UC-2-E4, UC-2-E5, UC-2-EC3, UC-4-E5, UC-22 (input parse), UC-33 (video_mode), FR-2.1–2.7, FR-15.1 · **TCs:** TC-3.1–TC-3.17, TC-4.13, TC-19.6
- **Files:** `src/shipcast/schemas.py` [new — `InputYaml`, `WalkthroughStep` models], `tests/unit/test_input_validation.py` [new — 13 reject + 2 accept]
- **Changes:** `InputYaml`: `repo_path: Path`, `entry_heading: str` (`min_length=1`), `live_url: AnyUrl | None`, `brand_slug: str`, `video_mode: Literal["standard","premium"]="standard"`, `feature_walkthrough: list[WalkthroughStep] | None`. `live_url` validators (in order; first match raises): scheme≠https; `socket.gethostbyname(host)` then reject `is_private`/`is_loopback`/`is_link_local`. `repo_path` validators: reject `..` segments; reject outside `/Users/aleksei/Documents/Projects.nosync/`; reject if no `CHANGELOG.md`; **resolve symlinks and re-check allowed-root after resolution** (TC-19.6). `WalkthroughStep`: `action: Literal["goto","click","type","wait","screenshot"]`, `selector`/`value` optional; reject `selector` containing `javascript:`. `schemas.py` MUST stay a leaf — no import of `cost`/`config`/`clients` (architect Module-Boundary Risk 1).
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/unit/test_input_validation.py`
- **Done when:** all 13 rejection cases raise `ValidationError` (http, ftp, 192.168.x, 10.x, 172.16.x, localhost, 169.254.x, `..` path, outside-root path, no-CHANGELOG path, unknown walkthrough action, `javascript:` selector, missing required field) (TC-3.1–TC-3.13); both standard+premium accept cases parse (TC-3.14/TC-3.15); `video_mode="veo3"` raises (TC-3.16); scheme rejections fire before `socket.gethostbyname` (monkeypatched to `AssertionError`) so no network call on scheme failures (TC-3.17); empty `entry_heading` raises (TC-4.13); symlink-escape repo_path rejected (TC-19.6).
- **Pre-review:** **security** — verify SSRF coverage of all RFC1918 ranges + loopback + link-local against the *resolved* IP (not the literal); verify `socket.gethostbyname` is monkeypatchable (no real network in tests); verify symlink-escape path-traversal defense.
- **Est. LoC:** ~120. ✓

### Slice 4 — Quality gates wired + manifest 100% coverage + Finding 2 decision
- **Wave:** 4
- **Covers:** NFR-16.1, NFR-16.2, **Architect MAJOR Finding 2** · **TCs:** TC-1.4, TC-1.5, TC-1.6, TC-1.7, TC-1.8, TC-1.9, TC-20.3, TC-22.1, TC-22.2, TC-22.3, TC-22.4
- **Files:** `tests/unit/test_manifest_hashes.py` [new — fill manifest to 100%], `tests/fixtures/manifests/v1_fresh.json` [new], `src/shipcast/stages/_base.py` [edit — document Finding 2 decision], `pyproject.toml` [edit — confirm `--cov-fail-under` gates], `tests/unit/test_inputs_hash_drift.py` [new — TC-20.3]
- **Changes:** **Architect MAJOR Finding 2 decision — adopt option (b):** `inputs_hash` is recorded for audit and powers `--rerun` invalidation; `BaseStage.check_inputs` does NOT auto-block on upstream `inputs_hash` drift (the human gate already forces a deliberate approve at every stage, bounding stale-input runs). Add an explicit docstring/comment block in `_base.py:check_inputs` stating this, and soften the over-claim in FR-1.24/FR-3.11 interpretation. TC-20.3 asserts the documented audit-only behavior (its option (b) branch). Round-trip byte-equality and both hash families covered to reach manifest 100%.
- **Verify:** `uv run mypy --strict src/shipcast && uv run ruff check src tests && uv run pytest --cov=shipcast.manifest --cov-fail-under=100 --cov-branch && uv run pytest --cov=shipcast --cov-fail-under=90 --cov-branch`
- **Done when:** all four gate commands exit 0 (TC-22.1–TC-22.4); manifest round-trip is byte-identical to the pinned fixture (TC-1.4); `compute_inputs_hash` is stable and mtime/size-sensitive (TC-1.5–TC-1.7); `compute_outputs_hash` detects same-size byte swaps and ignores mtime-only changes (TC-1.8/TC-1.9); TC-20.3 asserts the audit-only `inputs_hash` behavior with a comment reference in `_base.py`.
- **Pre-review:** none
- **Est. LoC:** ~90 (mostly tests + docstring). ✓

---

## Wave 2 — Input + Brand

### Slice 5 — `changelog/parser.py`
- **Wave:** 5
- **Covers:** UC-2, UC-2-A1, UC-2-A2, UC-2-EC2, UC-30, FR-4.1, FR-4.2 · **TCs:** TC-4.1, TC-4.2, TC-4.3, TC-4.4, TC-4.5, TC-4.6, TC-4.11, TC-21.4
- **Files:** `src/shipcast/changelog/parser.py` [new], `src/shipcast/schemas.py` [extend — `ChangelogEntry`], `tests/unit/test_changelog_parser.py` [new], `tests/fixtures/changelogs/{canonical,multi_per_day,missing_time,empty,no_dates}.md` [new]
- **Changes:** Hand-rolled scanner over `## YYYY-MM-DD` day headings and `### <name> — HH:MM UTC` entry headings; `ChangelogEntry{name,date,time|None,summary,details}`. Raises `ChangelogFileMissing` on absent file; never auto-creates. Heading match helper trims + lowercases.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/unit/test_changelog_parser.py`
- **Done when:** canonical fixture → populated `list[ChangelogEntry]` (TC-4.1); two `###` under one `##` → two entries same date (TC-4.2); missing-time → `time=None` (TC-4.3); empty file → `[]` (TC-4.4); no date headings → `[]` (TC-4.5); non-existent path → `ChangelogFileMissing`, no file created (TC-4.6); `"  add csv export  "` matches `"Add CSV Export"` (TC-4.11); same content parsed twice → byte-identical `json.dumps` (TC-21.4).
- **Pre-review:** none
- **Est. LoC:** ~100. ✓

### Slice 6 — `s01_pick` stage
- **Wave:** 6
- **Covers:** UC-2, UC-2-E1, UC-2-E2, UC-2-E3, UC-31, FR-4.3, FR-4.4, FR-4.5 · **TCs:** TC-4.7, TC-4.8, TC-4.9, TC-4.10, TC-4.12, TC-4.14, TC-21.1
- **Files:** `src/shipcast/stages/s01_pick.py` [new], `src/shipcast/stages/__init__.py` [edit — register in `ALL_STAGES`], `src/shipcast/cli.py` [edit — wire `pick` verb to stage], `tests/integration/test_s01_pick.py` [new], `tests/fixtures/repos/example_min/` [new — fixture repo with `CHANGELOG.md`]
- **Changes:** `check_inputs` reads + validates `input.yaml` via `InputYaml` (Slice 3) and confirms `input.yaml` present (else `StageInputMissing`); `run()` parses CHANGELOG, locates entry by case-insensitive trimmed heading match (else `ChangelogEntryNotFound`), writes deterministic `01_pick/entry.json`.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s01_pick.py`
- **Done when:** `shipcast pick <fixture> --entry "Add CSV export"` exits 0, writes schema-valid `01_pick/entry.json` byte-equal to fixture, `01_pick.status=="done"`, `human_approved_at=null` (TC-4.7); unknown heading → `ChangelogEntryNotFound`, `failed`, no `entry.json` (TC-4.8); no CHANGELOG → `ChangelogFileMissing` (TC-4.9); missing `input.yaml` → `StageInputMissing` before `run()` (TC-4.10); two runs → byte-identical `entry.json` (TC-4.12/TC-21.1); Review Checklist printed with abs path + ≥3 bullets + rerun/approve/reset text (TC-4.14).
- **Pre-review:** none
- **Est. LoC:** ~120. ✓

### Slice 7 — `s02_enrich` stage + `gemini_client.multimodal()` extension + Finding 3 decision
- **Wave:** 7
- **Covers:** UC-3, UC-3-A1, UC-3-A2, UC-3-E1..E5, UC-28, **Architect MAJOR Finding 3** · **TCs:** TC-5.1, TC-5.2, TC-5.3, TC-5.4, TC-5.5, TC-5.6, TC-5.7, TC-5.8, TC-5.9, TC-20.4 (TC-21.3 partial)
- **Files:** `src/shipcast/stages/s02_enrich.py` [new], `src/shipcast/clients/gemini_client.py` [extended — add `multimodal(prompt, images) -> str`; raise `GeminiRateLimited`], `src/shipcast/schemas.py` [extend — `EnrichedContext`], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `enrich`], `tests/integration/test_s02_enrich.py` [new], `tests/unit/test_gemini_multimodal.py` [new]
- **Changes:** Three sub-steps: (a) `gh pr list --json` + `git log --stat` in `repo_path` → `pr_links`,`diff_stats`; (b) Playwright walkthrough (skipped if `live_url` absent, logged) → `02_enrich/screenshots/*.png`; (c) `gemini_client.multimodal()` → narrative. `ba-analyst` via `subprocess.run(["claude","-p","ba-analyst",...], timeout=300)`; timeout→`SubagentTimeout`, non-zero→stderr in `error`, bad JSON→`SubagentMalformedOutput`. **Architect MAJOR Finding 3 decision — fold the narrative entirely into `context.json.narrative`; do NOT write a separate `narrative.md`** (single source of truth; no undeclared, un-hash-covered file). `EnrichedContext{pr_links,diff_stats,narrative,screenshots}`. `gemini_client` imports only `config`/`errors` (no `manifest`).
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s02_enrich.py tests/unit/test_gemini_multimodal.py`
- **Done when:** mocked happy path → schema-valid `02_enrich/context.json` with list[str] `pr_links`, dict `diff_stats`, non-empty `narrative`, ≥1 screenshot path (TC-5.1); `live_url=None` → `screenshots==[]`, narrative non-empty, playwright never called (TC-5.2); walkthrough absent but url present → 1 viewport screenshot (TC-5.3); subprocess TimeoutExpired → `SubagentTimeout`/`failed` (TC-5.4); non-zero exit → stderr captured (TC-5.5); non-JSON stdout → `SubagentMalformedOutput` (TC-5.6); multimodal raises `GeminiRateLimited` → fail, no `context.json` (TC-5.7); playwright raise → `PlaywrightTimeout`/`failed` (TC-5.8); **no `narrative.md` on disk and `context.json.narrative` is the sole copy; every written file appears in `outputs`** (TC-5.9/TC-20.4).
- **Pre-review:** none (sub-agent subprocess; no new auth/network surface beyond the already-validated `live_url`)
- **Est. LoC:** ~150. ✓

### Slice 8 — `playwright_client.py` (MCP wrapper) *(SECURITY PRE-REVIEW)*
- **Wave:** 8
- **Covers:** UC-4 (extraction), UC-4-E4, UC-4-E5, FR-3.4, FR-3.5, FR-3.6 · **TCs:** covered by Slice-10 integration (TC-6.9 PlaywrightTimeout, TC-6.10 URL-validator-before-goto) — Slice 8 lands the unit seam those exercise
- **Files:** `src/shipcast/clients/playwright_client.py` [new — adapted from the upstream scaffold`s `clients/playwright_client.py` pattern], `tests/unit/test_playwright_client.py` [new]
- **Changes:** `extract_css_palette(url)->list[str]` (top-5 hex by pixel frequency via PIL `Image.getcolors`), `extract_font_family(url)->str`, `screenshot_logo(url)->bytes|None`, `screenshot_feature(url, walkthrough)->list[Path]`. 60 s nav timeout → `PlaywrightTimeout`. The `InputYaml.live_url` validator (Slice 3) MUST be invoked before any `goto()` on every path. Imports only `config`/`errors`/`schemas` validators (no `manifest`).
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/unit/test_playwright_client.py`
- **Done when:** each method returns the expected shape against a mocked MCP transport; a bad/RFC1918-resolving URL raises `ValidationError` before the mocked transport is touched (asserted via mock-never-called); 60 s nav overrun raises `PlaywrightTimeout`.
- **Pre-review:** **security** — verify `live_url` validator runs BEFORE `goto()` on every code path; verify the 60 s navigation timeout is enforced and raises `PlaywrightTimeout`.
- **Est. LoC:** ~140. ✓

### Slice 9 — Extend `gemini_client.py` with `aspect_ratio`
- **Wave:** 9
- **Covers:** UC-4 (style sheet), UC-7, UC-11, FR-3.12 · **TCs:** covered by Slice-10/Slice-16 integration (AC-3.7 dimension assertions via TC-12.2); Slice 9 lands the unit regression
- **Files:** `src/shipcast/clients/gemini_client.py` [extended — `aspect_ratio: Literal["1:1","16:9","9:16","4:5","og"]` param; `og`→1200×630; default→16:9 for existing callers], `tests/unit/test_gemini_aspect.py` [new]
- **Changes:** Add `aspect_ratio` to `generate_image`; map each literal to pixel dims; preserve the single-HTTP-POST-per-call contract and the stage-owned retry loop (upstream-scaffold ruling); raise `GeminiTransientError`/`GeminiNonTransientError` (safety-block envelope) as in the upstream scaffold.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/unit/test_gemini_aspect.py`
- **Done when:** each `aspect_ratio` value yields the correctly-sized mocked PIL image; a call with no `aspect_ratio` still produces 16:9 (regression).
- **Pre-review:** none
- **Est. LoC:** ~60. ✓

### Slice 10 — `s03_brand` stage end-to-end + **Architect MAJOR Finding 1 (voice.md read-path)** *(SECURITY PRE-REVIEW)*
- **Wave:** 10
- **Covers:** UC-4, UC-4-A1..A4, UC-4-E1..E5, UC-4-EC1, UC-4-EC2, UC-15, UC-27-EC1, UC-33, **Architect MAJOR Finding 1** · **TCs:** TC-6.1–TC-6.16, TC-20.1, TC-20.2
- **Files:** `src/shipcast/stages/s03_brand.py` [new], `src/shipcast/brand/extractor.py` [new — composes Slice 8 + PIL palette], `src/shipcast/schemas.py` [extend — `BrandProposal`], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `brand`], `tests/integration/test_s03_brand.py` [new], `tests/fixtures/brand/test-brand/{voice.md,fonts/Inter.ttf,logo.svg}` [new], `tests/unit/test_brand_extractor.py` [new]
- **Changes:** `check_inputs` overrides `additional_input_paths` to pull `_brand/<slug>/` (the upstream scaffold`s `_base.py` line 153 hook); raises `BrandPackIncomplete` listing every missing REQUIRED file (`voice.md`, ≥1 `.ttf`, logo) before any API call. `run()`: call URL validator → Playwright extract (skip palette if `palette.hint.json`) → Gemini `style_sheet.png` (skip if brand-pack `style_sheet.png`) → logo (1×1 transparent + `logo_detected=false` if none). **Architect MAJOR Finding 1 — remediation option (a): `s03_brand.run()` copies `_brand/<slug>/voice.md` to `03_brand/voice.md` as a FOURTH declared output** so downstream `s04_plan`/`s08_video`/`s10_copy` read from `03_brand/voice.md` and `compute_outputs_hash` covers operator edits. Brand bytes NEVER touch `config_snapshot`.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s03_brand.py tests/unit/test_brand_extractor.py`
- **Done when:** complete pack (no hint) → `proposal.json`+`logo.png`+`style_sheet.png`+`voice.md`, valid PNG headers, schema-valid proposal (TC-6.1); missing `voice.md`/`fonts`/logo each → `BrandPackIncomplete` listing the file, no API call (TC-6.2–TC-6.5); `palette.hint.json` → `extract_css_palette` never called (TC-6.6); brand-pack `style_sheet.png` → `generate_image` never called (TC-6.7); no logo → 1×1 transparent PNG + `logo_detected=false` (TC-6.8); 60 s overrun → `PlaywrightTimeout` (TC-6.9); RFC1918-resolving url → `ValidationError` before any playwright call (TC-6.10); edit `proposal.json` then approve → `manually_edited=true` + changed-file list (TC-6.11); approve w/o edits → `manually_edited=false` (TC-6.12); `config_snapshot` byte-identical before/after (TC-6.13); replace all 3 outputs → all listed (TC-6.14); byte-identical replacement → no false edit (TC-6.15); **`03_brand/voice.md` exists, is in `manifest.stages.03_brand.outputs`, bytes match `_brand/<slug>/voice.md`** (TC-6.16/TC-20.1); removing `03_brand/voice.md` makes `s04_plan.check_inputs()` raise `StageInputMissing` (TC-20.2).
- **Pre-review:** **security** — verify operator-edit detection via `compute_outputs_hash`; verify NO brand bytes reach `config_snapshot` (byte-equality TC-6.13); verify copied `voice.md` is a declared, hash-covered output.
- **Est. LoC:** ~170. ✓

---

## Wave 3 — Creative scripting

### Slice 11 — `s04_plan` stage + `marketing/hooks.py` catalog + `brand-guardian` agent
- **Wave:** 11
- **Covers:** UC-5, UC-5-A1, UC-5-E1..E4, UC-12 (catalog), FR-6.1–6.6 · **TCs:** TC-7.1–TC-7.8, TC-22.5 (brand-guardian snapshot), TC-21.3 (partial)
- **Files:** `src/shipcast/stages/s04_plan.py` [new], `src/shipcast/marketing/hooks.py` [new — 7-template catalog + `render(key, entry)`], `src/shipcast/schemas.py` [extend — `MarketingBrief`, `StoryboardBeat`, `CarouselBeat`], `~/.claude/agents/brand-guardian.md` [new — user-level], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `plan`], `tests/integration/test_s04_plan.py` [new], `tests/unit/test_hooks_catalog.py` [new], `tests/unit/test_subagents.py` [new — brand-guardian snapshot]
- **Changes:** Chained sequential subprocesses: `planner` then `brand-guardian` (guardian's output is final), each 300 s timeout. `MarketingBrief`: `hook_template_per_channel: dict[Literal["x","linkedin","blog"], str]` (values ∈ 7 catalog keys), `ctas: list[str]` (≥1), `video_beats` len==4, `carousel_beats` len==4, `has_stat_card: bool`, `has_code_screenshot: bool`. `hooks.py` is a pure utility (no external API). `s04_plan` reads `03_brand/voice.md` (Finding-1 path). Uninstall note: `rm ~/.claude/agents/brand-guardian.md`.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s04_plan.py tests/unit/test_hooks_catalog.py tests/unit/test_subagents.py`
- **Done when:** mocked chain → schema-valid `04_plan/brief.json`, `video_beats`==4, `carousel_beats`==4, every hook value in catalog (TC-7.1); all 7 `hooks.render(key, sample)` non-empty (TC-7.2/AC-14.1); guardian's values override planner's (TC-7.3); planner timeout → `SubagentTimeout` no brief (TC-7.4); guardian timeout → `SubagentTimeout` (TC-7.5); bad JSON → `SubagentMalformedOutput` (TC-7.6); `video_beats` len 3 → `validate_outputs` fail (TC-7.7); `~/.claude/agents/brand-guardian.md` exists with `name`/`model`/`tools` frontmatter (TC-7.8); brand-guardian snapshot parses pinned mock stdout (TC-22.5).
- **Pre-review:** none
- **Est. LoC:** ~170. ✓ *(split-risk: catalog + stage; if >200 keep catalog tests minimal — catalog is pure data + 7 small render fns)*

### Slice 12 — `s05_script` stage + `demo-script-writer` agent
- **Wave:** 12
- **Covers:** UC-6, UC-6-E1..E3, FR-7.1–7.4 · **TCs:** TC-8.1–TC-8.8, TC-22.5 (demo-script-writer), TC-21.3 (partial)
- **Files:** `src/shipcast/stages/s05_script.py` [new], `src/shipcast/schemas.py` [extend — `Storyboard`], `~/.claude/agents/demo-script-writer.md` [new], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `script`], `tests/integration/test_s05_script.py` [new], `tests/unit/test_subagents.py` [extend — demo-script-writer snapshot]
- **Changes:** `demo-script-writer` via `claude -p` (300 s) with `brief.json`+`entry.json`; `Storyboard{beats: list[StoryboardBeat]}` each `{image_prompt,narration,duration_sec}`; beat count must be 4–6 inclusive else `SubagentMalformedOutput`. Uninstall: `rm ~/.claude/agents/demo-script-writer.md`.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s05_script.py tests/unit/test_subagents.py`
- **Done when:** 4-beat mock → schema-valid `05_script/storyboard.json` (TC-8.1); 6 beats accepted (TC-8.2); 3 or 7 beats → `SubagentMalformedOutput` (TC-8.3/TC-8.4); timeout → `SubagentTimeout` (TC-8.5); beat missing `narration` → `validate_outputs` fail (TC-8.6); agent file exists w/ valid frontmatter (TC-8.7); snapshot test asserts 4–6 beats + 3 fields (TC-8.8).
- **Pre-review:** none
- **Est. LoC:** ~110. ✓

---

## Wave 4 — Video assets and audio

### Slice 13 — `s06_video_assets` (both modes) + `veo_client.py` + ffmpeg Ken-Burns extension *(SECURITY PRE-REVIEW)*
- **Wave:** 13
- **Covers:** UC-7, UC-7-E1..E3, UC-8, UC-8-A1, UC-8-E1..E3, FR-8.1–8.8, FR-15.4 · **TCs:** TC-9.1–TC-9.8, TC-21.3 (partial) + the GAP test (`error.subtype=="GeminiSafetyBlocked"`, UC-7-E1)
- **Files:** `src/shipcast/stages/s06_video_assets.py` [new], `src/shipcast/clients/veo_client.py` [new — Veo 3 Fast REST; `VeoQuotaExceeded`/`VeoSafetyBlocked`/`VeoTimeout`(120 s)], `src/shipcast/clients/ffmpeg_client.py` [extended — `ken_burns_clip()`, 1080×1920 preset], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `video_assets`, `--no-veo` flag plumbed via stage param], `tests/integration/test_s06_video_assets.py` [new], `tests/unit/test_veo_client.py` [new]
- **Changes:** Declares `run()` with `_render_kenburns_clip`, `_render_veo_clip`, `_dispatch_beat(beat, mode)`. Standard: 4 beats Imagen(9:16)+Ken-Burns. Premium: beat[0] Veo Fast (conditioning `03_brand/style_sheet.png`), beats[1..3] Imagen+Ken-Burns. `VeoSafetyBlocked`→silent per-beat fallback, original prompt NEVER logged; `VeoQuotaExceeded`→fail (no further beats); `VeoTimeout`→fail. `--no-veo` forces premium beat[0] to Imagen+Ken-Burns via stage `run()` param (not via importing `cli`). `veo_client` imports only `config`/`errors`. Clips validated by `ffprobe`. Add the GAP unit test asserting `error.subtype=="GeminiSafetyBlocked"` on Imagen safety block.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s06_video_assets.py tests/unit/test_veo_client.py`
- **Done when:** standard → 4 MP4 1080×1920 h264 3–5 s, Veo never called (TC-9.1); premium → beat[0]=8 s, beats[1..3]=3–5 s, Veo called once (TC-9.2); `VeoSafetyBlocked` → Ken-Burns clip + blocked prompt absent from all log files (TC-9.3); `VeoQuotaExceeded` → fail, beats[1..3] absent (TC-9.4); `VeoTimeout` → fail (TC-9.5); `--no-veo` premium → all Ken-Burns, Veo never called (TC-9.6); bad codec → structured failure (TC-9.7); Imagen `GeminiRateLimited` → fail (TC-9.8); Imagen safety block → `error.subtype=="GeminiSafetyBlocked"` (GAP closure).
- **Pre-review:** **security** — verify `VeoSafetyBlocked` triggers per-beat fallback WITHOUT writing the original prompt to any log; verify 120 s polling timeout and quota path.
- **Est. LoC:** ~190. ✓ *(at limit — if it exceeds, split `veo_client.py` into its own commit ahead of the stage; keep the same wave)*

### Slice 14 — `s07_voice` stage
- **Wave:** 14
- **Covers:** UC-9, UC-9-E1..E3, FR-9.1–9.5 · **TCs:** TC-10.1–TC-10.6, TC-21.3 (partial)
- **Files:** `src/shipcast/stages/s07_voice.py` [new], `src/shipcast/clients/elevenlabs_client.py` [extended — `ElevenLabsQuotaExceeded` on 429], `src/shipcast/clients/whisperx_client.py` [copied], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `voice`], `tests/integration/test_s07_voice.py` [new], `tests/unit/test_s07_voice_errors.py` [new]
- **Changes:** Join `beat.narration` with single `\n` (no trailing); `synthesize(text, voice_id=settings.voice_id)`→`07_voice/narration.mp3`; `whisperx_client.transcribe()`→`07_voice/words.json`. `voice.md` constrains LLM tone only, never overrides `Settings.voice_id`. 429→`ElevenLabsQuotaExceeded` (no files). `shutil.which("whisperx") is None`→fail before synthesis.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s07_voice.py tests/unit/test_s07_voice_errors.py`
- **Done when:** mock → `narration.mp3` + non-empty `words.json`, word-duration sum within 1 s of mock MP3 duration (TC-10.1); joined text == `"A\nB\nC"` (TC-10.2); `synthesize` called with `Settings.voice_id` (TC-10.3); 429 → `ElevenLabsQuotaExceeded`, no files (TC-10.4); empty-key client → `MissingApiKey("ELEVENLABS_API_KEY")`, name only (TC-10.5); whisperx absent → descriptive fail, synth not called (TC-10.6).
- **Pre-review:** none
- **Est. LoC:** ~120. ✓

---

## Wave 5 — Video assembly

### Slice 15 — `s08_video` stage (full) + `composition/captions.py` + `composition/layout.py` + ffmpeg concat/loop extension
- **Wave:** 15
- **Covers:** UC-10, UC-10-A1..A4, UC-10-E1, **Architect MAJOR Finding 1 (voice.md consumer)**, FR-10.1–10.5, FR-14.8 · **TCs:** TC-11.1–TC-11.10
- **Files:** `src/shipcast/stages/s08_video.py` [new], `src/shipcast/composition/captions.py` [new — vendored from the upstream scaffold`s subtitle-burn renderer, reframed 1920×1080→1080×1920, add `karaoke`+`reveal`], `src/shipcast/composition/layout.py` [new — `draw_outlined` + grid/padding from `scripts/build_outro.py`], `src/shipcast/clients/ffmpeg_client.py` [extended — mixed-input concat, narration/bgm duck −3 dB, loop+GIF export], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `video`], `tests/integration/test_s08_video.py` [new], `tests/unit/test_captions_modes.py` [new]
- **Changes:** `_assemble_raw()` (concat 4 clips + narration + optional first-alphabetical `_brand/<slug>/music/*.mp3` ducked −3 dB), `_overlay_captions()` (mode from `03_brand/voice.md` `caption_mode:` line; default `chip` if absent/unrecognized), `_export_loop()` (first 6 s of beat[0], center-crop 1080×1080, no audio, mp4+gif). `composition/*` stay pure (no external API). `s08_video` reads `voice.md` from `03_brand/voice.md` (Finding-1 path). ffmpeg pre-flight via `requires_ffmpeg=True` (upstream-scaffold pattern).
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s08_video.py tests/unit/test_captions_modes.py`
- **Done when:** `showcase.mp4` 15–25 s 1080×1920 h264+aac; `loop_6s.mp4` 6.0±0.1 s 1080×1080 no-audio; `loop_6s.gif` ≤8 MB (TC-11.1); chip/karaoke/reveal each invoke the matching renderer (TC-11.2–TC-11.4); absent line → chip (TC-11.5); unrecognized value → chip, no exception (TC-11.6); no music → narration-only, no duck (TC-11.7); music present → −3 dB duck, first-alphabetical selected (TC-11.8); ≥95% caption-region frames differ from raw (TC-11.9); `voice.md` at `03_brand/voice.md` only → reads caption mode, no `FileNotFoundError` (TC-11.10).
- **Pre-review:** none
- **Est. LoC:** ~190. ✓ *(split-risk: if captions vendoring pushes >200, land `composition/captions.py`+`layout.py` as a preceding commit in the same wave, then `s08_video` + ffmpeg extension)*

---

## Wave 6 — Graphics

### Slice 16 — `s09_graphics` shell + 4 aspect cards + palette-conformance helper (Finding 7 mitigation)
- **Wave:** 16
- **Covers:** UC-11, UC-11-E1, UC-11-E2, FR-11.1, FR-11.2, FR-14.5 · **TCs:** TC-12.1 (partial), TC-12.2 (partial), TC-12.3, TC-12.10, TC-12.11
- **Files:** `src/shipcast/stages/s09_graphics.py` [new — full `run()` shell calling `_render_aspect_card`/`_render_og`/`_render_stat`/`_render_code`/`_render_carousel_slide`], `src/shipcast/composition/layout.py` [extend], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `graphics`], `tests/integration/test_s09_aspect_cards.py` [new], `tests/unit/test_palette_conformance.py` [new — reusable `assert_palette_conformance` helper]
- **Changes:** `_render_aspect_card(ratio)` = Gemini(ratio)+`draw_outlined` headline overlay with brand display font from `03_brand/`. Produce `1x1/16x9/9x16/4x5.png`. **Finding 7 (MINOR) mitigation: vendor a ~30-line pure-Python ΔE-CIE2000 (Sharma et al.) inside `tests/unit/test_palette_conformance.py` instead of importing `colormath`** (which calls the removed `numpy.asscalar`); remove `colormath` from `pyproject.toml` dependencies in this slice. Palette helper: PIL `quantize(colors=5)`, ≥80% pixels within ΔE<10 of `{primary,accent,neutral,#FFFFFF,#000000}`. Gemini call lives in the stage, never pushed into `marketing/` (architect Module-Boundary Risk 2).
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s09_aspect_cards.py tests/unit/test_palette_conformance.py`
- **Done when:** 4 cards at exact dims `1x1`(1080,1080)/`16x9`(1920,1080)/`9x16`(1080,1920)/`4x5`(1080,1350) (TC-12.2 partial); each passes ΔE-2000 palette conformance with pinned brief+proposal fixtures (TC-12.3); helper passes in-tolerance and raises on off-palette synthetic input (TC-12.11); `GeminiRateLimited` on 2nd call → `failed` (TC-12.10); `colormath` no longer in `pyproject.toml`.
- **Pre-review:** none
- **Est. LoC:** ~170 (incl. vendored ΔE-2000). ✓

### Slice 17 — `s09_graphics` OG card + conditional stat card
- **Wave:** 17
- **Covers:** UC-11, UC-11-A1, FR-11.3, FR-11.4, FR-11.5 · **TCs:** TC-12.4, TC-12.5
- **Files:** `src/shipcast/stages/s09_graphics.py` [edit — implement `_render_og`/`_render_stat`], `tests/unit/test_s09_og_stat.py` [new], `tests/fixtures/briefs/{stat_true,stat_false}.json` [new — pinned, NOT LLM output]
- **Changes:** `_render_og()` = Gemini(`og`→1200×630) + entry-name + logo overlay. `_render_stat(ratio)` for 4 ratios only when `brief.has_stat_card==true` → `stat_{1x1,16x9,9x16,4x5}.png`.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/unit/test_s09_og_stat.py`
- **Done when:** `og_card.png`==(1200,630) (TC-12.2 partial / AC-3.7); `has_stat_card=true` → 4 `stat_*.png` exist (TC-12.4); `has_stat_card=false` → no `stat_*.png`, `_render_stat` not called (TC-12.5).
- **Pre-review:** none
- **Est. LoC:** ~90. ✓

### Slice 18 — `s09_graphics` code screenshot + LinkedIn carousel + agents snapshot
- **Wave:** 18
- **Covers:** UC-11, UC-11-A2, UC-11-A3, FR-11.6–11.9 · **TCs:** TC-12.1, TC-12.2 (carousel), TC-12.6, TC-12.7, TC-12.8, TC-12.9
- **Files:** `src/shipcast/stages/s09_graphics.py` [edit — `_render_code`/`_render_carousel_slide`], `src/shipcast/marketing/code_screenshot.py` [new — Pygments+PIL Ray.so], `src/shipcast/marketing/carousel.py` [new — 6-slide composer], `tests/unit/test_code_screenshot.py` [new], `tests/integration/test_s09_carousel.py` [new], `tests/fixtures/briefs/{code_true,code_false,both_false}.json` [new]
- **Changes:** `_render_code()` (Pygments+PIL, no external API) only if `brief.has_code_screenshot==true`→`code.png`. Carousel: exactly 6 slides 1080×1350 (`slide_01`=hook from `hook_template_per_channel["linkedin"]`, `slide_02..05`=4 `carousel_beats`, `slide_06`=CTA). `marketing/*` pure.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/unit/test_code_screenshot.py tests/integration/test_s09_carousel.py`
- **Done when:** both flags true → 17 files present incl. `code.png` + 6 carousel slides (TC-12.1); `has_code_screenshot=true`→`code.png` openable, no external API (TC-12.6); false → no `code.png`, `_render_code` not called (TC-12.7); 6 carousel slides each (1080,1350), composer received hook for slide 01 and CTA for slide 06 (TC-12.8); both flags false → exactly 11 files (TC-12.9).
- **Pre-review:** none
- **Est. LoC:** ~180. ✓

---

## Wave 7 — Copy + Package

### Slice 19 — `s10_copy` stage + `social-copywriter` agent
- **Wave:** 19
- **Covers:** UC-12, UC-12-E1, UC-12-E2, **Architect MAJOR Finding 1 (voice.md consumer)**, FR-12.1–12.6 · **TCs:** TC-13.1–TC-13.9, TC-22.5 (social-copywriter), TC-21.3 (partial)
- **Files:** `src/shipcast/stages/s10_copy.py` [new], `src/shipcast/schemas.py` [extend — `CopyBundle`], `~/.claude/agents/social-copywriter.md` [new], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `copy`], `tests/integration/test_s10_copy.py` [new], `tests/unit/test_subagents.py` [extend — social-copywriter]
- **Changes:** `social-copywriter` via `claude -p` (300 s) with `brief.json`+`entry.json`+`context.json`+`03_brand/voice.md` (Finding-1 path). `CopyBundle{twitter_thread,linkedin,blog}`→3 `.md`. Lengths: twitter 3–8 numbered tweets ≤280 chars; linkedin 600–1200 words CommonMark; blog 1200–2000 words CommonMark. Each opens with its channel's hook template (substring of `hooks.render`). Twitter uses Unicode bold (no `**`); LinkedIn uses `→`/`▸` (no `-`/`*` markers). NO A/B variants (v1). Uninstall: `rm ~/.claude/agents/social-copywriter.md`.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s10_copy.py tests/unit/test_subagents.py`
- **Done when:** 3 files with correct lengths (TC-13.1); each opens with channel hook substring (TC-13.2); twitter no `**`, Unicode bold present (TC-13.3); linkedin `→`/`▸`, no `-`/`*` markers (TC-13.4); timeout → `SubagentTimeout`, no files (TC-13.5); 100-word blog → `validate_outputs` fail citing word-count (TC-13.6); agent file exists w/ frontmatter (TC-13.7); snapshot asserts CopyBundle shape + no `**` in twitter (TC-13.8); `03_brand/voice.md` passed to sub-agent, `_brand/<slug>/voice.md` not referenced (TC-13.9).
- **Pre-review:** none
- **Est. LoC:** ~150. ✓

### Slice 20 — `s11_package` stage
- **Wave:** 20
- **Covers:** UC-13, UC-13-E1, UC-31, FR-13.1–13.5 · **TCs:** TC-14.1–TC-14.5, TC-21.2
- **Files:** `src/shipcast/stages/s11_package.py` [new], `src/shipcast/schemas.py` [extend — `PackageManifest`], `src/shipcast/stages/__init__.py`+`cli.py` [edit — register `package`], `tests/integration/test_s11_package.py` [new]
- **Changes:** `release.zip` with all stage 01–10 outputs, entries sorted deterministically (byte-identical re-run). `README.md`: asset Markdown table (≥9 rows) + one fenced block per text channel (≥3). Conditional `stat_*.png`/`code.png` included when present. `code-reviewer` via `claude -p` (300 s) for README link sanity; timeout/parse-fail→`failed`.
- **Verify:** `uv run mypy --strict src/shipcast && uv run pytest -v tests/integration/test_s11_package.py`
- **Done when:** ZIP contains all required paths; README ≥3 fenced blocks + ≥9-row table (TC-14.1); conditional files included when present (TC-14.2) and absent when not produced (TC-14.3); re-run → byte-identical ZIP (TC-14.4/TC-21.2); `code-reviewer` timeout → `SubagentTimeout`/`failed` (TC-14.5).
- **Pre-review:** none
- **Est. LoC:** ~140. ✓

---

## Wave 8 — Integration + smoke + docs

### Slice 21 — Cost-ledger full-pipeline integration test
- **Wave:** 21
- **Covers:** UC-1, UC-34, FR-1.28, FR-15.2 · **TCs:** TC-17.3, TC-17.4, TC-23.1
- **Files:** `tests/integration/test_cost_cap.py` [new], `tests/integration/test_full_pipeline.py` [new — standard mode, all 11 stages mocked]
- **Changes:** Mock every external client; run all 11 stages standard mode via `CliRunner`, approve each; sum `metrics.cost_usd`. Premium variant with mock Veo $3.20.
- **Verify:** `uv run pytest -v tests/integration/test_cost_cap.py tests/integration/test_full_pipeline.py`
- **Done when:** standard pipeline total ≤ $3.00 (TC-17.3); premium total ≤ $8.00 (TC-17.4); full 11-stage standard run reaches all-done+approved, produces `release.zip`, no key values in `manifest.json` (TC-23.1).
- **Pre-review:** none
- **Est. LoC:** ~120 (tests only). ✓

### Slice 22 — E2E smoke vs real `example-project` (standard mode)
- **Wave:** 22
- **Covers:** UC-1 (real), AC-1.5 · **TCs:** TC-23.1 (real-data confirmation), TC-23.4
- **Files:** `docs/qa/shipcast_test_cases.md` [edit — record "Run 1" wall-clock + cost]; (no `src/shipcast/` change — operator-driven)
- **Changes:** Operator runs `shipcast pick ../example-project --entry "<latest>"` through all 11 gates standard mode; capture wall-clock (≤30 min) and cost (≤$3). **Finding 5 (MINOR):** when documenting the run, use canonical IDs (`shipcast brand`, `08_video/showcase.mp4`, `09_graphics/9x16.png`, `10_copy/twitter_thread.md`, `11_package/release.zip`) — not the stale appendix IDs.
- **Verify:** `uv run shipcast status example--<slug>` shows 11 done+approved rows; `unzip -l projects/example--<slug>/11_package/release.zip`
- **Done when:** all 11 stages `done`+approved on the real entry; `release.zip` contains showcase + loop + 4 aspect cards + OG + 6 carousel + 3 markdown; wall-clock ≤30 min; cost ≤$3 recorded under "Run 1" (TC-23.4).
- **Pre-review:** none
- **Est. LoC:** 0 production (docs only). ✓

### Slice 23 — E2E smoke (premium mode, one marquee entry)
- **Wave:** 23
- **Covers:** UC-8, AC-15.2 · **TCs:** TC-23.2
- **Files:** `docs/qa/shipcast_test_cases.md` [edit — record "Run 2"]
- **Changes:** Operator runs one premium-mode entry (mock-or-real Veo); capture wall-clock (≤45 min) and cost (≤$8) under "Run 2".
- **Verify:** `uv run shipcast status <slug>`; `ffprobe projects/<slug>/06_video_assets/beat_00.mp4` reports ~8 s
- **Done when:** premium run completes; `beat_00.mp4` ~8 s; total cost ≤$8 recorded under "Run 2" (TC-23.2).
- **Pre-review:** none
- **Est. LoC:** 0 production (docs only). ✓

### Slice 24 — CHANGELOG.md + README + PRD final pass (owned by `/merge-ready`)
- **Wave:** 24
- **Covers:** Deliverables checklist closure · **TCs:** none (release hygiene)
- **Files:** `CHANGELOG.md` [new — root], `README.md` [edit], `docs/PRD.md` [final pass — reflect Finding-1 4th output, Finding-2 audit-only wording, Finding-3 single-narrative]
- **Changes:** Per global changelog rule, `/merge-ready` writes the single entry with a real `date -u` timestamp after all gates PASS. Reconcile PRD wording for the three folded MAJOR findings.
- **Verify:** `uv run mypy --strict src/shipcast && uv run ruff check src tests && uv run pytest -v --cov=shipcast --cov-fail-under=90`
- **Done when:** all quality gates pass; `CHANGELOG.md` has one root entry with a real UTC timestamp; PRD wording matches the three folded findings.
- **Pre-review:** none
- **Est. LoC:** 0 production. ✓

---

## Acceptance criteria (verifiable "done" for the whole feature)

- `uv run shipcast --help` exits 0, lists 11 verbs, completes <1 s.
- `uv run mypy --strict src/shipcast`, `uv run ruff check src tests` → zero findings.
- `uv run pytest --cov=shipcast.manifest --cov-fail-under=100 --branch` and `--cov=shipcast --cov-fail-under=90 --branch` both exit 0.
- All 112 TCs pass; all 36 UCs exercised.
- `s08_video/showcase.mp4`: ffprobe 15–25 s, 1080×1920, h264+aac.
- `s09_graphics` emits the 4 aspect cards at exact named dims; all pass ΔE-2000 palette conformance.
- `s10_copy` emits 3 markdown files with channel-correct lengths, each opening with its brief-chosen hook.
- `s11_package/release.zip` byte-identical on re-run; README ≥3 fenced blocks + ≥9-row table.
- Standard-mode cost ≤$3, premium ≤$8, enforced by the dispatcher before each paid call.
- No API key value appears in `manifest.json` or any log file.
- Three architect MAJOR findings folded and asserted (TC-20.1/20.2 Finding 1, TC-20.3 Finding 2, TC-20.4 Finding 3).

## Files to create or change (master list)

- New core: `cost.py`, `schemas.py`, `changelog/parser.py`, `brand/extractor.py`, `marketing/{hooks,code_screenshot,carousel}.py`, `composition/{captions,layout}.py`, `clients/{playwright_client,veo_client}.py`, `stages/s01_pick.py … s11_package.py`.
- Copied/renamed from the upstream scaffold: `manifest.py`, `stage.py`, `stages/_base.py`, `cli.py`, `project.py`, `locking.py`, `errors.py`, `paths.py`, `logging_setup.py`, `config.py`, `audio.py`, `prompts.py`, `__main__.py`, `clients/{anthropic,elevenlabs,whisperx,ffmpeg}_client.py`.
- Extended: `clients/gemini_client.py` (`aspect_ratio`, `multimodal`), `clients/ffmpeg_client.py` (Ken-Burns, 1080×1920, concat-mixed, loop/GIF), `clients/elevenlabs_client.py` (`ElevenLabsQuotaExceeded`).
- Config: `config.toml` (remove stale `anthropic_model`), `pyproject.toml` (drop `colormath`).
- User-level agents: `~/.claude/agents/{brand-guardian,demo-script-writer,social-copywriter}.md`.
- Docs: `CHANGELOG.md`, `README.md`, `docs/PRD.md` final pass, `docs/qa/shipcast_test_cases.md` run logs.
- Tests: `tests/conftest.py` + `tests/unit/*` + `tests/integration/*` + `tests/fixtures/*` as enumerated per slice.

## Risk assessment

- **Data sensitivity:** API keys (`SecretStr`) — never serialized to `config_snapshot` or logs (Slice 2 enforces; TC-19.2/19.5). No PII, no financial data.
- **Auth impact:** No HTTP API/auth surface (Typer CLI). Sub-agents authenticate via the operator's `claude` CLI subscription.
- **Persistence changes:** Per-project `manifest.json` (atomic temp-rename), per-stage artifacts under `projects/<slug>/`. No DB, no schema migration beyond `schema_version:1`. Brand bytes never enter `config_snapshot`.
- **External calls:** Gemini (Imagen/multimodal/Veo Fast), ElevenLabs, Playwright (`live_url` — SSRF-validated), `gh`/`git` (read-only in target repo), `claude -p` sub-agents. Cost-capped. **Security-flagged slices: 2, 3, 8, 10, 13.**
- **shipcast never writes into the target repo** — reads CHANGELOG + runs `gh`/`git log` only.

## Dependencies

- Libraries (existing `pyproject.toml`): typer, pydantic v2, pydantic-settings, pyyaml, rich, pillow, playwright, pygments, requests, elevenlabs, jinja2. `whisperx` extra (openai-whisper, torch<2.3, numba<0.62, numpy<2). **Remove `colormath`** (Slice 16; vendor pure-Python ΔE-2000).
- Tools/env: `claude` CLI on PATH, ffmpeg 8.x, Playwright MCP (`~/.claude/playwright-mcp-laconit.json`), macOS/Linux. `example-project` must have a `CHANGELOG.md` and reachable live URL for Slice 22.
- No new pip dependency added (Rule 4 escalation avoided): the only proposed dependency *change* is removing `colormath` — net subtraction, not addition.

---

## Traceability summary (slice → UCs → TCs; all 112 TCs owned)

| Slice | UCs | TCs owned |
|---|---|---|
| 1 | UC-1,2,14–19,21–27,29,32,35,36 | TC-1.1,1.2,1.3,1.10–1.17,1.11,2.1–2.6,15.1–15.6,16.1–16.9,18.1–18.4,19.1,19.3,19.4,19.5,23.3,23.4 |
| 2 | UC-20,34,36 | TC-17.1,17.2,17.5,17.6,19.2 |
| 3 | UC-2-E4/E5/EC3,4-E5,33 | TC-3.1–3.17,4.13,19.6 |
| 4 | Finding 2 | TC-1.4–1.9,20.3,22.1–22.4 |
| 5 | UC-2,2-A1/A2/EC2,30 | TC-4.1–4.6,4.11,21.4 |
| 6 | UC-2,2-E1/E2/E3,31 | TC-4.7–4.10,4.12,4.14,21.1 |
| 7 | UC-3,3-A1/A2,3-E1–E5,28,Finding 3 | TC-5.1–5.9,20.4 |
| 8 | UC-4 extract,4-E4/E5 | (unit seam; asserted via TC-6.9,6.10) |
| 9 | UC-4 stylesheet,7,11 | (unit regression; asserted via TC-12.2/AC-3.7) |
| 10 | UC-4,4-A1–A4,4-E1–E5,4-EC1/EC2,15,27-EC1,33,Finding 1 | TC-6.1–6.16,20.1,20.2 |
| 11 | UC-5,5-A1,5-E1–E4,12(catalog) | TC-7.1–7.8,22.5 |
| 12 | UC-6,6-E1–E3 | TC-8.1–8.8,22.5 |
| 13 | UC-7,7-E1–E3,8,8-A1,8-E1–E3 | TC-9.1–9.8 (+GeminiSafetyBlocked GAP) |
| 14 | UC-9,9-E1–E3 | TC-10.1–10.6 |
| 15 | UC-10,10-A1–A4,10-E1,Finding 1 | TC-11.1–11.10 |
| 16 | UC-11,11-E1/E2 | TC-12.3,12.10,12.11 |
| 17 | UC-11,11-A1 | TC-12.4,12.5 |
| 18 | UC-11,11-A2/A3 | TC-12.1,12.2,12.6,12.7,12.8,12.9 |
| 19 | UC-12,12-E1/E2,Finding 1 | TC-13.1–13.9,22.5 |
| 20 | UC-13,13-E1,31 | TC-14.1–14.5,21.2 |
| 21 | UC-1,34 | TC-17.3,17.4,23.1 |
| 22 | UC-1 real | TC-23.1(real),23.4 |
| 23 | UC-8 premium | TC-23.2 |
| 24 | deliverables | — |
| cross-cutting | NFR-16.6 | TC-21.3 (slices 7,11,12,13,14,19); TC-22.5 (slices 11,12,19) |

**Orphan-TC check:** All 112 TCs are owned. TC-12.1/12.2/12.9 are jointly owned by Slices 16–18 (the s09 shell spans those three slices); they appear under Slice 18 above where the final file set completes. Slices 8 and 9 own no *directly-numbered* TC (the test-case doc verifies them through Slice-10/Slice-16 integration: TC-6.9/6.10 for Playwright, TC-12.2/AC-3.7 for Gemini aspect) — this is intentional component decomposition, not an orphan. **No uncovered TCs.** The one documented coverage GAP (UC-7-E1 `GeminiSafetyBlocked` subtype) is closed by a dedicated assertion added in Slice 13.

## Wave / sequencing diagram (8 waves, sequential-within)

```
Wave 1  Scaffold + invariants        Slice 1 → 2 → 3 → 4
Wave 2  Input + brand                Slice 5 → 6 → 7 → 8 → 9 → 10
Wave 3  Creative scripting           Slice 11 → 12
Wave 4  Video assets + audio         Slice 13 → 14
Wave 5  Video assembly               Slice 15
Wave 6  Graphics                     Slice 16 → 17 → 18
Wave 7  Copy + package               Slice 19 → 20
Wave 8  Integration + smoke + docs   Slice 21 → 22 → 23 → 24
```
Each wave depends only on outputs of earlier waves; within a wave, slices run in listed order because they all edit `stages/__init__.py::ALL_STAGES` + `cli.py`. No slice depends on a later slice's output (verified against the architecture review's traced dependency chain).

---

## Report

- **Total slices:** 24 (across 8 waves).
- **Waves:** 8, sequential-within (registry + CLI shared-file constraint).
- **TCs not yet owned by a slice:** none — all 112 owned (Slices 8 and 9 verified via Slice-10/Slice-16 integration TCs, not orphans). The single documented GAP (UC-7-E1 `GeminiSafetyBlocked` subtype) is closed by an added assertion in Slice 13.
- **All 3 architect MAJOR findings folded in:**
  - **Finding 1 (voice.md read-path)** → **Slice 10** (option a: `s03_brand` copies `_brand/<slug>/voice.md` to `03_brand/voice.md` as a 4th declared output; consumers in Slices 11/15/19 read that path) — asserted by TC-20.1, TC-20.2, TC-6.16, TC-11.10, TC-13.9.
  - **Finding 2 (`inputs_hash` re-check)** → **Slice 4** (decision: option b — `inputs_hash` is audit-only / powers `--rerun`, documented in `_base.py`; human gate bounds stale-input runs) — asserted by TC-20.3.
  - **Finding 3 (narrative.md vs context.json)** → **Slice 7** (decision: fold narrative entirely into `context.json.narrative`; no separate `narrative.md`) — asserted by TC-20.4, TC-5.9.
- **MINOR findings folded:** Finding 4 (stale `anthropic_model`) → Slice 2; Finding 5 (stale appendix IDs) → Slice 22 doc step; Finding 6 ("06 images" label) → noted, scaffold already correct (`06_video_assets/`); Finding 7 (colormath/numpy.asscalar) → Slice 16 (drop `colormath`, vendor pure-Python ΔE-2000).
- **One discrepancy flagged for implementation:** TC-19.3 expects `AnthropicClient(api_key=...)` to raise `MissingApiKey`, but the reused `AnthropicClient` uses the `claude` CLI subscription and takes no key (the upstream scaffold`s `anthropic_client.py`). Restrict that assertion to the two real-key clients (Gemini, ElevenLabs) and document Anthropic's subscription model — noted in Slice 1.
