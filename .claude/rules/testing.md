# Testing Rules

## TDD by default

- Tests first; minimum implementation to pass.
- One slice = one set of new/modified tests + the production code that satisfies them.
- Test files mirror the source tree: `src/shipcast/manifest.py` ⇒ `tests/unit/test_manifest.py`.

## Frameworks

- `pytest` 8.x with `pytest-cov`.
- `tmp_path` fixture for any test that touches the filesystem.
- Typer's `CliRunner` for CLI tests; do not spawn subprocesses for unit-level CLI tests.
- Subprocess-based tests (`subprocess.Popen`) are reserved for the two-process concurrency race only.

## No real external API calls in unit tests

- Anthropic, ElevenLabs, Gemini (Imagen + multimodal + Veo), WhisperX — never instantiated for real in any unit test.
- Stage tests inject mock clients via `clients_factory: Callable[[], Clients]`. The default `clients_factory` reads `Settings` and creates real clients; tests override it.
- Playwright — never driven against a real URL in unit tests. Use a mock that returns canned screenshots/CSS values.
- ffmpeg — the `FfmpegClient.check_available()` test may shell out to a real `ffmpeg -version` since it's available on dev machines, but `s08_video` assembly tests must inject a mock that records calls.

## Shared fixtures

- `tests/conftest.py` exposes `tmp_project_root` (a `tmp_path`-based fresh root directory) and any other cross-cutting fixtures (sample brand pack, sample changelog, pinned `MarketingBrief` fixture for graphics tests).
- Per-module fixtures live in `tests/<dir>/conftest.py`.
- JSON / YAML fixtures live in `tests/fixtures/` (manifests, sample projects, sample inputs, brief.json snapshots for deterministic graphics tests).

## Determinism

- Tests must not depend on wall-clock time. Use `freezegun` or inject a `now: Callable[[], datetime]` parameter when the production code uses `datetime.now()`.
- Tests must not depend on file `mtime` ordering across tests. The shared `tmp_project_root` fixture gives each test a fresh root.
- Graphics conditionality tests (Slice 17/18 stat card and code screenshot) use **pinned brief.json fixtures** with explicit `has_stat_card` / `has_code_screenshot` values — NOT live `planner` LLM output.

## Pre_run_hook race-test seam

- The two-process race test injects `BaseStage.pre_run_hook` to sleep. Production code MUST NOT branch on any test-only env var like `SHIPCAST_TEST_SLEEP_SEC`.

## Coverage gates

- `src/shipcast/manifest.py` must reach 100% line coverage (enforced by `pytest --cov=shipcast.manifest --cov-fail-under=100` in Slice 4).
- Package overall must reach ≥ 90% (enforced by `pytest --cov=shipcast --cov-fail-under=90` in Slice 4).
- Coverage is computed with branch tracking (`branch = true` in `pyproject.toml`).

## Snapshot / byte-equality tests

- Manifest round-trip tests assert byte-equality, not just JSON equality.
- Deterministic stage idempotency tests (parsing, schema, composition) assert byte-equality of artifact files across re-runs.

## Sub-agent snapshot tests

- The three project-specific sub-agents (`brand-guardian`, `demo-script-writer`, `social-copywriter`) each ship with a fixed-scenario snapshot test in `tests/unit/test_subagents.py`. The test mocks the `claude -p` subprocess call and asserts the parsing of expected output structure (JSON shape, length constraints).

## Negative assertions matter

- Tests should assert what MUST NOT happen as well as what must (no extra files created, no manifest mutation when an error path is taken, no log lines from production code reading env vars, no `config_snapshot` mutation in any stage after `s01_pick`).

## Palette conformance test (graphics)

- Each generated graphic is PIL-`quantize(colors=5)`'d, then ≥ 80% of its pixels must fall within ΔE-CIE2000 < 10 of one of: brand `primary`, `accent`, `neutral`, pure white (#FFFFFF), pure black (#000000).
- Implemented in `tests/unit/test_palette_conformance.py` as a reusable helper; called by graphics integration tests.
