# Security Rules

## Secrets handling

- API keys live in `.env` (gitignored). The repo only ships `.env.example` with key names and empty values — no real keys, no comments containing keys. A test in `tests/unit/test_package_imports.py` enforces this with a grep-style scan.
- `Settings` (pydantic-settings) reads `.env` and exposes API keys via `SecretStr`. Never `.get_secret_value()` outside of the client `__init__` that needs to authenticate.
- `config_snapshot` written into `manifest.json` MUST exclude every `SecretStr` field. Implement by separating `Settings` into a "public" subset (model ids, voice ids, durations, costs) and a "secret" subset (API keys); only the public subset is serialized into the snapshot.

## Lazy client construction

- Clients (`anthropic_client`, `elevenlabs_client`, `gemini_client`, `veo_client`, `playwright_client`, `ffmpeg_client`) are constructed inside `stage.run()`. Never at module import, never at CLI startup.
- Client `__init__` raises `MissingApiKey("<KEY_NAME>")` when the relevant `SecretStr` is empty. The exception message includes the key NAME only, never the (empty) value.
- The CLI dispatcher does NOT eagerly construct any client.

## Input validation (URL + path)

Slice 3 enforces in `InputYaml` Pydantic schema:

- `live_url`:
  - `https://` scheme only — `http://`, `ftp://`, `file://`, `javascript:` rejected
  - Hostname rejected if `ipaddress.IPv4Address(socket.gethostbyname(host)).is_private` is True
  - `ipaddress.IPv4Address.is_loopback` rejected
  - `ipaddress.IPv4Address.is_link_local` rejected
  - This prevents SSRF when Playwright is pointed at the `live_url`

- `repo_path`:
  - Must be under `/Users/aleksei/Documents/Projects.nosync/`
  - No `..` segments
  - Must exist and contain a `CHANGELOG.md`

- `feature_walkthrough[].selector`:
  - Reject `javascript:` schemes
  - Reject unknown actions (only `goto`, `click`, `type`, `wait`, `screenshot` allowed)

## Logging

- `logging_setup.configure(project)` initializes a `RichHandler` (console) plus a JSON-line file handler under `<project>/logs/<YYYYMMDDTHHMMSSZ>.log`.
- Tracebacks written to the log file MUST NOT include `SecretStr` raw values. Pydantic's `SecretStr.__repr__` masks them; do not `.get_secret_value()` inside any log call.
- Settings objects logged for debugging MUST go through `Settings.model_dump(mode='python')` — Pydantic redacts SecretStr in dumps by default; confirm before logging.

## `.lock` foot-gun guard

- `--no-lock` bypasses concurrency protection. It is ONLY honored when env var `SHIPCAST_NO_LOCK_ACK=1` is also set. Without the ack, the dispatcher raises `LockBypassNotAcknowledged` and exits non-zero.
- Even with the ack, the dispatcher prints a yellow warning banner before proceeding.

## Filesystem

- All shipcast artifacts are confined to `projects/<slug>/`. Stages MUST NOT write outside their declared `stage_dir`.
- `Project.create` validates the slug is a safe directory name (alphanumeric, hyphens, underscores) before any filesystem write.
- shipcast NEVER writes into the target software project (e.g. `getdeal-platform-monorepo/`). It reads `CHANGELOG.md` and runs `gh` / `git log` in that directory, but never modifies it. CHANGELOG-missing → error, NEVER auto-create.

## Pre-review checkpoints

The architect's review flags these slices for security pre-review during implementation:

- **Slice 2** — cost ledger + dispatcher gate. Verify accumulated cost cannot underflow and that cap check is atomic with respect to manifest updates.
- **Slice 3** — input.yaml validators. Verify SSRF defenses against all RFC1918 ranges including IPv6 unique-local + reserved; verify path-traversal defenses including symlink escape.
- **Slice 8** — Playwright client. Verify URL validator is called BEFORE Playwright `goto()`; verify navigation timeout enforced.
- **Slice 10** — `s03_brand` end-to-end. Verify operator-edit detection works correctly via `compute_outputs_hash`; verify no brand bytes leak into `config_snapshot`.
- **Slice 13** — Veo client + standard-mode fallback. Verify `VeoSafetyBlocked` triggers per-beat fallback without leaking the original prompt to logs.
