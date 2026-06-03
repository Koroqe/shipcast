# shipcast E2E Smoke Runbook (Slices 22 & 23)

> **Audience:** the operator (a human with real API keys and a real spend budget).
> **Why this exists:** the automated suite proves the pipeline is correctly wired
> end-to-end with EVERY external client mocked
> (`tests/integration/test_full_pipeline_smoke.py`, both modes). It does NOT — and
> cannot — exercise the *real* Gemini / ElevenLabs / Veo / `claude` calls, the
> *real* Playwright navigation against a live URL, or real wall-clock / cost. Those
> are the two acceptance smokes below, which only the operator can run.
>
> **These runs are PENDING operator execution.** Fill in the Run 1 / Run 2 result
> rows after executing them.

---

## What each run proves

| Run | Slice | Mode | Acceptance gate |
|-----|-------|------|-----------------|
| Run 1 | 22 | `standard` | All 11 stages `done`+approved on a real `example-project` entry; `release.zip` carries the full asset set; **wall-clock ≤ 30 min, cost ≤ $3.00**. |
| Run 2 | 23 | `premium`  | One marquee entry through premium mode; `06_video_assets/beat_00.mp4` is the ~8 s Veo hero clip; **wall-clock ≤ 45 min, cost ≤ $8.00**. |

The cost cap is enforced by the dispatcher BEFORE every paid call: a standard run
is hard-capped at $3.00 and a premium run at $8.00 (`CostCapExceeded` aborts the
stage before any paid API is touched). Wall-clock is measured by the operator.

---

## Prerequisites (both runs)

1. **API keys in `.env`** (repo root, gitignored). Required, non-empty:
   ```
   GEMINI_API_KEY=...
   ELEVENLABS_API_KEY=...
   ```
   `claude -p` sub-agents (ba-analyst / planner / brand-guardian /
   demo-script-writer / social-copywriter / code-reviewer) authenticate via the
   operator's local `claude` CLI **subscription** — no Anthropic API key is read.
2. **User-level sub-agents installed** under `~/.claude/agents/`:
   `brand-guardian.md`, `demo-script-writer.md`, `social-copywriter.md`
   (plus the built-in `planner` / `ba-analyst` / `code-reviewer`).
3. **`claude` CLI on PATH** and logged into a working subscription
   (`claude -p --agent planner "ping"` returns without an auth error).
4. **Binaries on PATH:** `ffmpeg`, `ffprobe`, `whisperx` (the voice stage
   pre-flights `whisperx`; absent → the stage fails fast before any synthesis),
   and `gh` + `git` (read-only repo signals; degrade gracefully if `gh` is
   unauthenticated).
5. **Target repo:** `../example-project/` exists under
   `/Users/aleksei/Documents/Projects.nosync/` and contains a `CHANGELOG.md`
   with the entry you intend to pick. shipcast NEVER writes into the target repo.
6. **A reachable `https` `live_url`** (optional but recommended) for the brand /
   enrich Playwright extraction. It MUST be public — RFC1918 / loopback /
   link-local hosts are rejected by the SSRF validator. Omit it to skip the
   Playwright sub-steps (provide a `palette.hint.json` in the brand pack instead).
7. **Brand pack populated** at `projects/_brand/acme/` (the `--brand-slug`):
   - `voice.md` (REQUIRED — tone, banned phrases, CTA pattern, `caption_mode:` line)
   - `fonts/` with ≥ 1 `.ttf` display font (REQUIRED)
   - `logo.svg` (or `logo.png` with transparency) (REQUIRED)
   - `palette.hint.json` `{primary, accent, neutral}` (OPTIONAL — skips Playwright
     palette extraction; recommended when no `live_url`)
   - `music/*.mp3` (OPTIONAL — ducked −3 dB under narration)
   - `style_sheet.png` (OPTIONAL — skips the Gemini style-sheet image call)

   An incomplete pack raises `BrandPackIncomplete` (listing every missing required
   file) at `shipcast brand`, BEFORE any paid call.

---

## The human gate (applies to every stage)

Every stage finishes in `done` and the NEXT stage refuses to run until you
approve the current one:

```sh
shipcast approve <slug> <stage_id>
```

Between a run and its approval you may hand-edit the stage's outputs (e.g. trim
`03_brand/proposal.json` down to exactly 3 hex colors, or swap `logo.png`); the
approve step recomputes the byte-content hash and records `manually_edited=true`
plus the changed files. Use `shipcast <verb> <slug> --rerun` to redo a stage and
`shipcast reset <slug> <stage_id> --yes` to roll back (both refuse to discard
downstream approvals without `--yes`).

The derived project slug is `<repo-short>--<entry-slug>`, e.g.
`example-project--add-csv-export`. Use `shipcast status <slug>` (a
read-only, no-lock view) to watch progress.

---

## Run 1 — standard mode (Slice 22)

Pick the latest CHANGELOG entry, then walk all 11 gates in `standard` mode.

```sh
# 0. Start a stopwatch (wall-clock is an acceptance gate).

# 1. Create the project + run 01_pick (heading is the text between '### ' and ' — HH:MM UTC').
shipcast pick ../example-project \
  --entry "Investor onboarding website auto-fill" \
  --brand-slug acme \
  --video-mode standard
  # --live-url https://app.example.com   # optional, public https only

shipcast approve <slug> 01_pick

# 2. Walk the remaining 10 stages: run → review → approve, in order.
shipcast enrich        <slug> && shipcast approve <slug> 02_enrich
shipcast brand         <slug> && shipcast approve <slug> 03_brand
shipcast plan          <slug> && shipcast approve <slug> 04_plan
shipcast script        <slug> && shipcast approve <slug> 05_script
shipcast video_assets  <slug> && shipcast approve <slug> 06_video_assets
shipcast voice         <slug> && shipcast approve <slug> 07_voice
shipcast video         <slug> && shipcast approve <slug> 08_video
shipcast graphics      <slug> && shipcast approve <slug> 09_graphics
shipcast copy          <slug> && shipcast approve <slug> 10_copy
shipcast package       <slug> && shipcast approve <slug> 11_package

# 3. Stop the stopwatch. Verify.
shipcast status <slug>                                   # 11 done + approved rows
unzip -l projects/<slug>/11_package/release.zip          # asset set present
```

**Expected `release.zip` contents** (standard, no stat/code cards):
`08_video/showcase.mp4`, `08_video/loop_6s.mp4`, `08_video/loop_6s.gif`,
`09_graphics/{1x1,16x9,9x16,4x5}.png`, `09_graphics/og_card.png`,
`09_graphics/carousel/slide_0{1..6}.png`,
`10_copy/{twitter_thread,linkedin,blog}.md`.
(If the brief set `has_stat_card` / `has_code_screenshot`, the corresponding
`09_graphics/stat_*.png` / `09_graphics/code.png` also appear.)

**Reading the cost:** sum `metrics.cost_usd` across stages in
`projects/<slug>/manifest.json`, or note that the dispatcher would have aborted
with `CostCapExceeded` had the projected total crossed $3.00.

### Run 1 — result (PENDING operator execution)

| Field | Value |
|-------|-------|
| Entry heading picked | _PENDING_ |
| Project slug | _PENDING_ |
| Date / operator | _PENDING_ |
| Wall-clock (mm:ss) — gate ≤ 30 min | _PENDING_ |
| Total cost (USD) — gate ≤ $3.00 | _PENDING_ |
| All 11 stages done + approved? | _PENDING_ |
| `release.zip` asset set complete? | _PENDING_ |
| Notes / deviations | _PENDING_ |

---

## Run 2 — premium mode (Slice 23)

Same flow on ONE marquee entry, with `--video-mode premium`. Premium renders
`beat[0]` of `06_video_assets` as an 8 s Veo 3 Fast hero clip (≈ $3.20) and keeps
`beats[1..3]` on the Imagen + Ken-Burns path; the cap rises to $8.00.

> Set `default_mode = "premium"` in `config.toml` (or accept that the cost CAP is
> derived from `config.toml`, not `input.yaml`): the per-project RENDER mode comes
> from `input.yaml.video_mode` (set by `--video-mode premium`), but the cost CAP
> comes from `settings.video_mode`. Both must read `premium` or the Veo $3.20 call
> would be blocked by the standard $3 cap.

```sh
# 0. Start a stopwatch.

shipcast pick ../example-project \
  --entry "<marquee entry heading>" \
  --brand-slug acme \
  --video-mode premium

shipcast approve <slug> 01_pick
# … walk 02_enrich → 11_package exactly as in Run 1, approving each …

# Verify the Veo hero clip + duration.
ffprobe projects/<slug>/06_video_assets/beat_00.mp4      # ~8.0 s
shipcast status <slug>                                   # 11 done + approved
```

`--no-veo` forces the standard (all Ken-Burns) path even for a premium project —
useful to stay in the cheaper cost band for a premium-tagged entry, but then
Run 2's Veo gate is not exercised, so do NOT pass it for the acceptance run.

### Run 2 — result (PENDING operator execution)

| Field | Value |
|-------|-------|
| Entry heading picked | _PENDING_ |
| Project slug | _PENDING_ |
| Date / operator | _PENDING_ |
| Wall-clock (mm:ss) — gate ≤ 45 min | _PENDING_ |
| Total cost (USD) — gate ≤ $8.00 | _PENDING_ |
| `06_video_assets/beat_00.mp4` ≈ 8 s (Veo hero)? | _PENDING_ |
| All 11 stages done + approved? | _PENDING_ |
| Total cost exceeds $3 (premium genuinely needed)? | _PENDING_ |
| Notes / deviations | _PENDING_ |

---

## Troubleshooting

- **`BrandPackIncomplete`** at `shipcast brand` → a required brand-pack file is
  missing; the error lists each one. No paid call was made.
- **`CostCapExceeded`** → the projected total would cross the mode cap; the gated
  stage is `failed` and no paid call was made. Reset/rerun cheaper stages or
  switch modes.
- **`StageInputMissing` for `whisperx`** at `shipcast voice` → install `whisperx`
  on PATH; synthesis was NOT attempted (no ElevenLabs spend).
- **`SubagentTimeout` / `SubagentFailed`** → the `claude -p` sub-agent timed out
  (300 s) or exited non-zero; confirm the `claude` CLI is logged in and the agent
  files exist under `~/.claude/agents/`.
- **`ValidationError` on `live_url`** → the URL is not public https (SSRF
  defense). Use a public host or omit `--live-url` and rely on `palette.hint.json`.
- **Lock contention (`ProjectLocked`)** → another `shipcast` process holds the
  project lock; wait for it or, knowingly, run with `--no-lock`
  (`SHIPCAST_NO_LOCK_ACK=1` required).
