# Changelog

All notable changes to this project, newest first. Entries are grouped by UTC date.

## 2026-06-03

### Build shipcast v1 — auto-marketing factory — 09:54 UTC
**Summary:** shipcast can now turn a single changelog entry from any software project into a complete, on-brand marketing package — a showcase video, social posts, a blog post, and graphics — with a person approving each step.
**Details:** Delivers the full 11-stage human-gated pipeline (pick → enrich → brand → plan → script → video assets → voice → video → graphics → copy → package). Two video modes: standard ($3 cap, image + Ken-Burns motion) and premium ($8 cap, adds one Veo 3 Fast hero clip). Output: 9:16 showcase video, looping clip, four aspect cards, OG card, optional stat/code cards, a 6-slide LinkedIn carousel, X/LinkedIn/blog copy, and a paste-ready release.zip. Built across 24 slices; 600 tests pass.
**Technical details:** New Python CLI (`shipcast`) reusing the 5-minute-library factory scaffold: a single `manifest.json` per project as source of truth, atomic writes, two hash families, and a human approval gate at every stage. Adds Gemini Imagen/multimodal/Veo, ElevenLabs, WhisperX, Playwright, and ffmpeg clients (lazily constructed, keys never serialized), three new marketing sub-agents (brand-guardian, demo-script-writer, social-copywriter), input-validation (SSRF + path-traversal) and a per-project cost ledger that aborts before exceeding the cap. Security pre-reviews passed on the cost gate, input validators, Playwright navigation, brand stage, and the Veo path. No deployment or database — local, macOS/Linux only. Real-API end-to-end smoke is operator-driven (see docs/qa/shipcast_e2e_runbook.md).
