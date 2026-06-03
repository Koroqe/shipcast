# shipcast — Project Guidance for Claude Code

## What this project is

A Python auto-marketing factory that turns one `CHANGELOG.md` entry from any target software project into a complete marketing package — showcase video, X thread, LinkedIn long-form, blog post, static graphics — through an **11-stage human-gated pipeline**. Each entry lives in `projects/<slug>/` with a single `manifest.json` as the source of truth. The CLI is `shipcast`; each stage is one verb. The operator approves between stages.

## Pipeline stages

1. `01_pick` — read CHANGELOG.md entry, write `01_pick/entry.json`
2. `02_enrich` — `gh` + `git log` + Playwright screenshots → Gemini multimodal narrative
3. `03_brand` — Playwright extracts palette/fonts/logo; operator hand-edits `proposal.json` before approve
4. `04_plan` — marketing brief: hook-template choice per channel, CTAs, 4-beat storyboard, 4 carousel beats, stat/code flags
5. `05_script` — showcase storyboard (4 beats × 3–5 s) + voiceover lines
6. `06_video_assets` — 4 video clips. Standard mode: Imagen still + Ken-Burns × 4. Premium mode: Veo 3 Fast × 1 + Imagen + Ken-Burns × 3
7. `07_voice` — ElevenLabs narration + Whisper word timestamps
8. `08_video` — ffmpeg concat + caption overlay + 6-second loop + GIF export
9. `09_graphics` — 4 aspect cards + OG card + (conditional) stat card + (conditional) code screenshot + LinkedIn carousel (6 slides)
10. `10_copy` — X thread + LinkedIn + blog markdown via `social-copywriter` sub-agent
11. `11_package` — `release.zip` + README with paste-blocks

Each stage declares `id`, `requires`, `output_schema`, and `review_checklist_items`. The CLI dispatcher — not stages — owns manifest writes, locking, and human-gate enforcement.

## Two video modes (`input.yaml.video_mode`)

| Mode | Cost cap | Hero shot | Fill shots |
|---|---|---|---|
| `standard` (default) | $3/entry | Gemini Imagen + Ken-Burns | Gemini Imagen + Ken-Burns × 3 |
| `premium` (opt-in) | $8/entry | Veo 3 Fast (8 s motion + audio) | Gemini Imagen + Ken-Burns × 3 |

Veo 3 Fast pricing is honest ($3.20/clip with audio); we use it sparingly. Premium mode is for marquee launches.

## Brand pack contract

`projects/_brand/<brand_slug>/` MUST contain or `s03_brand` raises `BrandPackIncomplete`:

```
voice.md          REQUIRED — tone, banned phrases, signature phrases, CTA pattern, motion style, caption_mode
fonts/            REQUIRED — ≥ 1 .ttf (display); optional 2nd (body)
logo.svg          REQUIRED — SVG preferred; PNG with transparency acceptable
palette.hint.json OPTIONAL — pre-seeded {primary, accent, neutral}; skips Playwright extract entirely (faster + safer)
music/            OPTIONAL — 1–3 .mp3/.wav for bgm
style_sheet.png   OPTIONAL — operator-supplied if Gemini-generated style is undesirable
```

## Visual style contract (brand-guardian sub-agent enforces)

- **Typography:** 2 fonts max, default Inter (NOT Arial). Operator's `_brand/<slug>/fonts/*.ttf` overrides.
- **Color:** exactly 3 hex codes after operator edit of `proposal.json` — `primary`, `accent`, `neutral`. 60-30-10 rule on quantize-to-5 palette histogram.
- **Palette conformance:** PIL `quantize(colors=5)`; ≥ 80% pixels within ΔE-CIE2000 < 10 of {primary, accent, neutral, white, black}.
- **Spacing:** 8-point grid; ≥ 8% padding on every static graphic.
- **Motion:** slow push-in / smooth transitions unless `voice.md` says otherwise.
- **Captions:** `chip` (default) | `karaoke` | `reveal`, picked via `voice.md` line `caption_mode: <name>`.

## Architecture invariants (do not break)

- **src-layout:** all engine code lives under `src/shipcast/`. Tests live under `tests/`. Per-entry artifacts live under `projects/<slug>/`.
- **Manifest is single source of truth.** No sidecar JSONs. Atomic temp-rename writes. Deterministic JSON serialization (`sort_keys=True`, `indent=2`, trailing newline).
- **Two distinct hash families:**
  - `compute_inputs_hash` — fast `(path, mtime_ns, size)` SHA-256. Used for `inputs_hash` (upstream invalidation).
  - `compute_outputs_hash` — byte-content SHA-256. Used for `outputs_hash_at_done` and `shipcast approve` edit detection. Asymmetry is load-bearing.
- **Lazy client construction.** External clients (Anthropic, ElevenLabs, Gemini, Veo, WhisperX, ffmpeg) are instantiated inside `stage.run()` only. API key validation happens in client `__init__`. Stage tests inject mocks via `clients_factory`.
- **Cascade-confirmation guard.** `shipcast <verb> --rerun` and `shipcast reset` against a stage with approved downstream stages refuse without `--yes`, listing every approval that will be discarded.
- **Human gate at every stage.** Each stage finishes in `done` status. The next stage refuses to run until `shipcast approve <slug> <stage_id>` records `human_approved_at`. Operators can `--rerun`, hand-edit outputs, or `reset` between runs and approvals.
- **Platform:** macOS and Linux only. Other platforms raise `UnsupportedPlatform` at CLI startup. Concurrency uses `fcntl.flock`. `--no-lock` requires `SHIPCAST_NO_LOCK_ACK=1` env var.
- **Brand data NEVER mutates `config_snapshot`.** `03_brand` outputs live as files; downstream stages read them; `inputs_hash` covers drift. This avoids the `ConfigSnapshotLocked` violation that a "brand_lock" stage would cause.

## Input validation (security-critical)

Slice 3 enforces in `InputYaml` Pydantic schema:
- `live_url` https-only, RFC1918 / loopback / link-local rejected via `ipaddress.IPv4Address.is_private`
- `repo_path` under `/Users/aleksei/Documents/Projects.nosync/`, no `..`
- `feature_walkthrough[].selector` rejects `javascript:` schemes and unknown actions

## Commit conventions

- **Feature branches only.** Never commit on `main`.
- **Conventional Commits.** Format: `<type>(<scope>): <message>`.
- **Allowed types:** `feat`, `fix`, `test`, `chore`.
- **Allowed scopes:** `api | ui | db | auth | core | infra`.
- **Scope mapping for this project:**
  - Any file under `src/shipcast/` ⇒ scope `core`. This rule wins even when the same commit also touches docs, tests, or configs.
  - Otherwise (pyproject.toml, .gitignore, .env.example, config.toml, README.md, docs/**, .claude/**, tests/**-only-changes) ⇒ scope `infra`.
- **Never add AI attribution** ("Co-Authored-By: Claude", "Generated by ...", etc.) to commit messages. The commit message must contain only the change description.
- One slice = one atomic commit. No squashing across slices.

## Documentation pipeline

Every feature follows the global autonomous pipeline:
1. `/bootstrap-feature` — produces `docs/PRD.md` section, `docs/use-cases/<feature>_use_cases.md`, architecture review, `docs/qa/<feature>_test_cases.md`, and `docs/qa/<feature>_implementation_plan.md`.
2. `/implement-slice` — TDD per slice, atomic commits, scratchpad updates.
3. `/merge-ready` — typecheck, ruff, pytest with coverage gates, end-to-end smoke.

## Testing

- TDD. Tests first; minimum implementation to pass.
- `pytest` + `tmp_path` fixtures. Shared fixtures live in `tests/conftest.py`.
- No real external API calls in unit tests. Integration tests inject mock clients via `clients_factory`.
- Coverage gates: `manifest.py` 100%, package overall ≥ 90%.
- Concurrency race tests must use `BaseStage.pre_run_hook` (test-injected) rather than reading any env var inside production code.

## Quality gates

Before declaring a slice done, all of:

```sh
uv sync
uv run mypy --strict src/shipcast
uv run ruff check src tests
uv run pytest -v
```

must report zero failures.

## Reference plan

Full 24-slice implementation plan lives at `/Users/aleksei/.claude/plans/okay-so-currently-i-unified-canyon.md`.
