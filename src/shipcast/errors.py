"""Shipcast-specific exception hierarchy.

All shipcast exceptions inherit from `ShipcastError`. Slice 2 defines manifest-related
exceptions; later slices will extend with stage/project/CLI exceptions.
"""

from __future__ import annotations


class ShipcastError(Exception):
    """Base class for all shipcast exceptions."""


class ManifestCorrupt(ShipcastError):
    """The manifest file is unreadable, unparseable, or fails schema validation."""


class ManifestMigrationNeeded(ShipcastError):
    """The manifest's `schema_version` does not match the current version."""


class IllegalTransition(ShipcastError):
    """A stage status transition is not in the allowed transition matrix."""


class CannotApproveNonDoneStage(ShipcastError):
    """`approve` was called on a stage whose status is not `done`."""


class ConfigSnapshotLocked(ShipcastError):
    """`config_snapshot` cannot be modified because at least one stage is no longer `pending`."""


class UnknownStageId(ShipcastError):
    """An operation referenced a stage_id that does not exist in the manifest."""


class ProjectExists(ShipcastError):
    """`Project.create` was called for a slug whose folder already exists (without `force=True`)."""


class ProjectNotFound(ShipcastError):
    """`Project.load` was called for a slug whose folder does not exist."""


class InvalidSlug(ShipcastError):
    """A slug failed validation (must be alphanumeric with hyphens or underscores)."""


class StageInputMissing(ShipcastError):
    """An upstream stage's output file is missing on disk OR a required local input is absent."""


class StageNotApproved(ShipcastError):
    """An upstream stage is `done` but has not been explicitly approved by the operator."""


class StageOutputInvalid(ShipcastError):
    """A stage's run produced outputs that fail Pydantic validation or are missing files."""


class ChangelogFileMissing(ShipcastError):
    """A target project's ``CHANGELOG.md`` does not exist on disk.

    Raised by ``changelog.parser.parse_changelog`` (and ``s01_pick``). shipcast
    NEVER auto-creates the changelog — a missing file is always an operator error.
    """


class ChangelogEntryNotFound(ShipcastError):
    """No changelog entry matched the requested ``entry_heading``.

    Raised by ``s01_pick`` after a trimmed, case-insensitive match over every
    parsed :class:`~shipcast.schemas.ChangelogEntry` name fails.
    """


class BrandPackIncomplete(ShipcastError):
    """The operator's ``projects/_brand/<brand_slug>/`` pack is missing a REQUIRED file.

    Raised by ``s03_brand.check_inputs`` BEFORE any external API call (Playwright
    or Gemini). The brand-pack contract (``.claude/CLAUDE.md``) REQUIRES, at
    minimum, ``voice.md``, at least one ``.ttf`` in ``fonts/``, and a logo
    (``logo.svg`` or ``logo.png``). The exception message LISTS every missing
    REQUIRED item so the operator can fix them in one pass (FR-3.3 / TC-6.2..6.5).

    ``missing`` is preserved as a named attribute (a sorted tuple of the missing
    item labels) so tests can assert on the set independently of the message.
    """

    def __init__(self, brand_slug: str, missing: tuple[str, ...]) -> None:
        self.brand_slug = brand_slug
        self.missing = missing
        listed = ", ".join(missing)
        super().__init__(
            f"brand pack for {brand_slug!r} is incomplete; missing required "
            f"file(s): {listed}"
        )


class UnsupportedPlatform(ShipcastError):
    """The current OS is not supported by the shipcast CLI (macOS/Linux only)."""


class ProjectLocked(ShipcastError):
    """Another shipcast process holds the project's `.lock` file."""


class LockBypassNotAcknowledged(ShipcastError):
    """`--no-lock` was passed without `SHIPCAST_NO_LOCK_ACK=1` acknowledgement."""


class FfmpegNotFound(ShipcastError):
    """`ffmpeg` binary is not on `PATH`."""


class FfmpegAssembleFailed(ShipcastError):
    """`ffmpeg` exited non-zero during Stage 10's video-assembly subprocess call.

    Raised by ``shipcast.clients.ffmpeg_client.assemble`` when the subprocess
    returns a non-zero exit code. The stage's dispatcher records this in the
    manifest's error block per FR-1.19, so the operator can diagnose without
    re-running the encode.

    The message format is ``"ffmpeg exit {returncode}: {stderr_tail}"`` —
    architect Ruling 8 caps ``stderr_tail`` at the last 2000 characters of
    ffmpeg's captured stderr, which is enough to retain the final error
    line plus 1-2 preceding warning lines without bloating ``manifest.json``.
    Named attributes (``self.returncode`` / ``self.stderr_tail``) are
    preserved so tests can assert on them independently of the message text.
    """

    def __init__(self, returncode: int, stderr_tail: str) -> None:
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        super().__init__(f"ffmpeg exit {returncode}: {stderr_tail}")


class CostCapExceeded(ShipcastError):
    """The next paid API call would push accumulated cost over the project cap.

    Raised by the dispatcher as a TRUE pre-condition — BEFORE the stage's
    `run()` (and therefore before any paid client is constructed or invoked).
    The stage transitions to `failed` with `error.type = "CostCapExceeded"`
    and the accumulated cost does not increase (UC-20 / FR-1.28). The check is
    strict (`projected > cap`): a projected total exactly equal to the cap is
    allowed.
    """


class MissingApiKey(ShipcastError):
    """An external-API client was instantiated without its required key.

    The exception message includes the env-var NAME only (e.g.,
    `"ANTHROPIC_API_KEY"`), never the value.
    """


class GeminiTransientError(ShipcastError):
    """Gemini AI Studio returned a transient HTTP failure (429/500/502/503/504).

    Raised by `GeminiClient.generate_image` so the stage's retry loop can
    classify the error and decide whether to retry. The stage wraps a final
    retry-exhausted failure in `GeminiImageGenFailed`.
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Gemini AI Studio returned transient HTTP {status_code}: {body}"
        )


class GeminiNonTransientError(ShipcastError):
    """Gemini AI Studio returned a non-transient error (HTTP 4xx or content policy).

    Raised by `GeminiClient.generate_image` for HTTP 400/401/403/404 and for
    HTTP 200 responses that carry a safety-block envelope. The stage's retry
    loop MUST NOT retry these — the stage wraps them in `GeminiImageGenFailed`
    immediately so the operator sees the error on the first attempt.
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Gemini AI Studio returned non-transient HTTP {status_code}: {body}"
        )


class GeminiImageGenFailed(ShipcastError):
    """Stage 09 failed to generate one scene's image after exhausting retries.

    Wraps the underlying cause (a `GeminiTransientError` after retry
    exhaustion, a `GeminiNonTransientError`, or a `ValueError` raised by
    the inline JPEG magic-byte check). The message always identifies the
    `scene_index` so the operator can locate the failure point.
    """

    def __init__(self, scene_index: int, cause: BaseException) -> None:
        self.scene_index = scene_index
        self.cause = cause
        super().__init__(
            f"Stage 09 image generation failed for scene {scene_index}: {cause}"
        )


class GeminiRateLimited(ShipcastError):
    """Gemini returned HTTP 429 (rate limit / quota) on a multimodal call.

    Raised by `GeminiClient.multimodal` (Slice 7) when the AI Studio
    ``generateContent`` surface responds with status 429. Distinct from the
    image-generation `GeminiTransientError` family: the *narrative* call has no
    stage-owned retry loop (one shot per `s02_enrich` run), so a 429 is a
    terminal failure that surfaces directly through the dispatcher's FAILED
    transition with ``error.type == "GeminiRateLimited"`` (TC-5.7 / AC-5.4).
    The message carries the (truncated) response body but never any API key.
    """

    def __init__(self, body: str = "") -> None:
        self.body = body
        super().__init__(f"Gemini multimodal call rate-limited (HTTP 429): {body}")


class SubagentTimeout(ShipcastError):
    """A `claude -p` sub-agent subprocess exceeded its wall-clock timeout.

    Raised by stages that drive Claude sub-agents (`s02_enrich`'s ba-analyst,
    `s04_plan`'s planner/brand-guardian, etc.) when `subprocess.run` raises
    `subprocess.TimeoutExpired` (300 s budget). Surfaces through the FAILED
    transition with ``error.type == "SubagentTimeout"`` (TC-5.4 / UC-28).
    The message identifies the agent so the operator knows which call stalled.
    """


class SubagentFailed(ShipcastError):
    """A `claude -p` sub-agent subprocess exited non-zero.

    Raised when the `claude` CLI returns a non-zero exit code (auth failure,
    model error, content policy, etc.). The captured ``stderr`` tail is folded
    into the message so the operator can diagnose without re-running (TC-5.5).
    ``stderr`` is preserved as a named attribute for independent assertions.
    """

    def __init__(self, agent: str, returncode: int, stderr: str) -> None:
        self.agent = agent
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"sub-agent {agent!r} exited {returncode}: {stderr.strip()[:500]}"
        )


class SubagentMalformedOutput(ShipcastError):
    """A `claude -p` sub-agent exited 0 but its stdout was not valid JSON.

    Raised when an agent that is contracted to emit JSON returns un-parseable
    output. Surfaces through the FAILED transition with
    ``error.type == "SubagentMalformedOutput"`` (TC-5.6 / UC-28-A2).
    """


class PlaywrightTimeout(ShipcastError):
    """A Playwright navigation/screenshot exceeded its timeout budget.

    Raised by `PlaywrightClient` methods (Slice 8) and propagated by
    `s02_enrich` when feature-walkthrough screenshots time out. Surfaces
    through the FAILED transition with ``error.type == "PlaywrightTimeout"``
    (TC-5.8 / UC-3-E5 / FR-3.5).
    """


class PlaywrightBrowserNotInstalled(ShipcastError):
    """Chromium binary is absent from the playwright-managed install location.

    Raised by `PlaywrightClient.__init__` when
    `playwright.chromium.executable_path()` returns an empty string or a path
    that does not exist on disk. The exception message includes the remediation
    command (`playwright install chromium`) so the operator knows exactly what
    to run.
    """


class PlaywrightLoginRequired(ShipcastError):
    """The persistent browser profile is not authenticated to YouTube Studio.

    Raised by `PlaywrightClient.upload_video` when the Studio page title does
    not contain 'YouTube Studio' after navigation (the session cookie has
    expired and the browser redirected to Google sign-in).
    """


class PlaywrightProfileBusy(ShipcastError):
    """Another process holds Chromium's SingletonLock on the persistent profile.

    Raised by `PlaywrightClient.upload_video` / `click_schedule` when Chromium
    reports "user data directory is already in use". Remediation: close the
    other open browser window or wait for the concurrent `shipcast publish`
    invocation to finish.
    """


class PlaywrightSessionLost(ShipcastError):
    """The Gate 2 re-attach could not locate the uploaded video in Studio.

    Raised by `PlaywrightClient.click_schedule` when navigating to the video's
    Studio edit URL times out or the video is no longer in a schedulable state
    (deleted, already scheduled from the UI, etc.).
    """


class PlaywrightUploadFailed(ShipcastError):
    """A Playwright action failed during the upload-form sequence.

    Raised by `PlaywrightClient.upload_video` when a locator action exhausts
    its per-action retry budget. Carries the step label and the wrapped
    original exception for diagnostic purposes.

    Message format (Slice 3a hardening): the original exception's message is
    truncated to the first line, capped at 200 chars, to prevent DOM dumps
    from bloating logs and manifests. The full original is preserved in
    ``self.original`` for debugging.

    ``partial_upload_state`` records whether a draft video was created on
    YouTube before the failure, so ``s11_publish.run()`` can write the correct
    value into ``upload_log.json`` for operator cleanup guidance (ruling R-9).
    """

    def __init__(
        self,
        step_name: str,
        original: Exception,
        *,
        partial_upload_state: str = "none",
    ) -> None:
        self.step_name = step_name
        self.original = original
        self.partial_upload_state = partial_upload_state
        # Truncate to first line, max 200 chars, to bound message size.
        truncated = str(original).splitlines()[0][:200]
        super().__init__(
            f"Playwright upload failed at step '{step_name}': "
            f"{original.__class__.__name__}: {truncated}"
        )


class DurationOutOfTolerance(ShipcastError):
    """narration.mp3 duration is outside the target ± tolerance window.

    Stage 04 raises this from `run()` when `abs(actual - target) > tolerance`.
    The message is the load-bearing operator-facing artifact (FR-4.4):
    contains the numeric values AND both remediation paths so the operator
    can choose whether to rerun stage 02 (change the script length) or
    stage 03 (change the voice/model). The `±` symbol is Unicode U+00B1
    (`b"\\xc2\\xb1"` in UTF-8).
    """

    def __init__(
        self,
        *,
        actual: float,
        target: float,
        delta: float,
        tolerance: float,
    ) -> None:
        self.actual = actual
        self.target = target
        self.delta = delta
        self.tolerance = tolerance
        super().__init__(
            f"Audio duration out of tolerance: "
            f"actual={actual:.2f}s, target={target:.2f}s, "
            f"delta={delta:+.2f}s, tolerance=±{tolerance:.2f}s. "
            f"Remediation: rerun Stage 02 (shipcast script <slug> --rerun) "
            f"to generate a shorter/longer script, OR rerun Stage 03 "
            f"(shipcast voice <slug> --rerun) if settings changed."
        )
