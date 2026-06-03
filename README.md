# shipcast

Auto-marketing factory. Turns one `CHANGELOG.md` entry from any software project into a complete on-brand marketing package — showcase video, X thread, LinkedIn long-form post, blog post, and static graphics — through an 11-stage human-gated pipeline.

Status: **v1 complete.** All 11 stages implemented and wired end-to-end (24 slices across 8 waves). Quality gates green — `mypy --strict`, `ruff`, 600 tests passing, `manifest.py` at 100% coverage, package overall at 91%. The real-API end-to-end smoke is operator-driven — see [docs/qa/shipcast_e2e_runbook.md](docs/qa/shipcast_e2e_runbook.md).

## Quick start

```sh
uv sync
uv run shipcast --help
uv run shipcast pick ../example-project --entry "Add CSV export" --brand acme
# ... operator approves at each of 11 gates ...
open projects/example--csv-export/11_package/release.zip
```

## Pipeline (11 stages)

```
01 pick      → register one changelog entry as a project
02 enrich    → gh + git log + Playwright screenshots → Gemini multimodal narrative
03 brand     → Playwright extract palette/fonts/logo → operator-edited proposal.json
04 plan      → marketing brief (hook templates, CTAs, storyboard, carousel beats)
05 script    → showcase storyboard (4 beats × 3–5 s)
06 video_assets → 4 video clips (standard: Imagen+Ken-Burns; premium: Veo 3 Fast × 1 + 3 Ken-Burns)
07 voice     → ElevenLabs narration + Whisper word timestamps
08 video     → ffmpeg concat + caption overlay + loop+gif export
09 graphics  → 4 aspect cards + OG card + (conditional) stat/code + LinkedIn carousel (6 slides)
10 copy      → X thread + LinkedIn + blog markdown
11 package   → release.zip + README with paste-blocks
```

## Two video modes

| Mode | Cost cap | Hero shot | Fill shots |
|---|---|---|---|
| `standard` (default) | $3/entry | Gemini Imagen + Ken-Burns | Gemini Imagen + Ken-Burns × 3 |
| `premium` (opt-in) | $8/entry | Veo 3 Fast (8 s motion + audio) | Gemini Imagen + Ken-Burns × 3 |

## Brand pack contract

`projects/_brand/<brand_slug>/` MUST contain (or `s03_brand` raises `BrandPackIncomplete`):

```
voice.md          REQUIRED — tone, banned phrases, signature phrases, CTA pattern, motion style, caption_mode
fonts/            REQUIRED — ≥ 1 .ttf (display); optional 2nd (body)
logo.svg          REQUIRED — SVG preferred; PNG with transparency acceptable
palette.hint.json OPTIONAL — pre-seeded {primary, accent, neutral} (skips Playwright extract)
music/            OPTIONAL — 1–3 .mp3/.wav for bgm
style_sheet.png   OPTIONAL — operator-supplied if Gemini-generated style is undesirable
```

## Tooling

| Tool | Use | Pipeline stage |
|---|---|---|
| Gemini Imagen | All static graphics + style sheet + standard-mode video stills | s03, s06, s09 |
| Gemini 2.5 Pro multimodal | Visual-understanding pass on Playwright screenshots | s02 |
| Gemini Veo 3 Fast | Premium-mode hero motion clip (opt-in) | s06 (premium) |
| ElevenLabs v3 | Voiceover | s07 |
| OpenAI Whisper | Word-level timestamps for caption sync | s07 |
| Playwright MCP | Brand auto-extract + real-app feature screenshots | s02, s03 |
| Pygments + PIL | Local code-screenshot renderer (Ray.so style) | s09 |
| ffmpeg | Video assembly + Ken-Burns + caption overlay + concat + GIF export | s06, s08 |

## Quality gates

```sh
uv sync
uv run mypy --strict src/shipcast
uv run ruff check src tests
uv run pytest -v
```

Coverage gates: `shipcast.manifest` 100%, package overall ≥ 90%.

## Plan

See `docs/qa/shipcast_implementation_plan.md` for the full implementation plan (24 slices across 8 waves).
