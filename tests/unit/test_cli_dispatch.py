"""CLI dispatcher tests.

Owned TCs:
- TC-2.1: `shipcast --help` exits 0 and lists all 11 verb names.
- TC-2.2: `shipcast status` renders a stage table.
- TC-2.3: `Project.create` writes a manifest with all 11 stages pending.
- TC-2.4: `projects/_template/` has all 11 stage subdirectories.
- TC-19.3: client `__init__` raises MissingApiKey with the key NAME only;
  AnthropicClient (subscription model) constructs without a key.
- TC-19.4: no client is constructed at CLI startup (lazy construction).
- TC-23.3: a stage `run()` raising → manifest transitions to FAILED + saved.

Slice-1 note: every verb resolves to "not yet implemented" until a stage is
injected, so dispatch tests inject `FakeStage` via `shipcast.stages.ALL_STAGES`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import shipcast.cli as cli
import shipcast.stages as _stages
from shipcast.config import Settings
from shipcast.errors import MissingApiKey
from shipcast.manifest import Manifest, StageStatus
from shipcast.paths import STAGE_DIR_NAMES, STAGE_IDS, default_template_path
from shipcast.project import Project
from shipcast.stage import StageResult
from tests._fakestage import FakeStage

runner = CliRunner()

_EXPECTED_VERBS = (
    "pick",
    "enrich",
    "brand",
    "plan",
    "script",
    "video_assets",
    "voice",
    "video",
    "graphics",
    "copy",
    "package",
)


def _seed_project(tmp_path: Path) -> Project:
    return Project.create(
        tmp_path, "entry", {}, settings=Settings(), template_path=default_template_path()
    )


# --------------------------------------------------------------------------- #
# TC-2.1 — --help lists all 11 verbs
# --------------------------------------------------------------------------- #


def test_tc_2_1_help_exits_zero_and_lists_eleven_verbs() -> None:
    """TC-2.1: `shipcast --help` exits 0 and lists all 11 pipeline verbs."""
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    for verb in _EXPECTED_VERBS:
        assert verb in result.output, f"verb {verb!r} missing from --help"
    # status / approve / reset are also present.
    for cmd in ("status", "approve", "reset"):
        assert cmd in result.output


def test_tc_2_1_verb_count() -> None:
    """TC-2.1: exactly 11 pipeline verbs are registered."""
    assert len(cli._VERB_TO_STAGE_ID) == 11
    assert tuple(cli._VERB_TO_STAGE_ID.values()) == STAGE_IDS


# --------------------------------------------------------------------------- #
# TC-2.3 / TC-2.4 — template + project creation
# --------------------------------------------------------------------------- #


def test_tc_2_4_template_has_eleven_stage_dirs() -> None:
    """TC-2.4: projects/_template/ has all 11 stage subdirectories."""
    template = default_template_path()
    for dir_name in STAGE_DIR_NAMES.values():
        assert (template / dir_name).is_dir(), f"missing template dir {dir_name}"


def test_tc_2_4_template_manifest_all_pending() -> None:
    """TC-2.4: the seeded template manifest has all 11 stages pending."""
    m = Manifest.load(default_template_path() / "manifest.json")
    assert set(m.stages) == set(STAGE_IDS)
    assert all(r.status == StageStatus.PENDING for r in m.stages.values())


def test_tc_2_3_project_create_writes_eleven_pending_stages(tmp_path: Path) -> None:
    """TC-2.3: Project.create writes a manifest with all 11 stages pending."""
    project = _seed_project(tmp_path)
    m = Manifest.load(project.manifest_path)
    assert set(m.stages) == set(STAGE_IDS)
    assert all(r.status == StageStatus.PENDING for r in m.stages.values())
    assert m.slug == "entry"
    assert m.entry is None


def test_project_create_records_entry(tmp_path: Path) -> None:
    """Project.create stores the optional `entry` dict in the manifest."""
    project = Project.create(
        tmp_path,
        "entry",
        {},
        settings=Settings(),
        entry={"name": "Add CSV export"},
        template_path=default_template_path(),
    )
    m = Manifest.load(project.manifest_path)
    assert m.entry == {"name": "Add CSV export"}


# --------------------------------------------------------------------------- #
# TC-2.2 — status table
# --------------------------------------------------------------------------- #


def test_tc_2_2_status_renders_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-2.2: `shipcast status` renders a row for the (injected) stage registry."""
    _seed_project(tmp_path)
    monkeypatch.setattr(_stages, "ALL_STAGES", (FakeStage,))
    result = runner.invoke(cli.app, ["--projects-root", str(tmp_path), "status", "entry"])
    assert result.exit_code == 0, result.output
    assert "01_pick" in result.output
    assert "pending" in result.output


def test_status_empty_registry_is_fine(tmp_path: Path) -> None:
    """status with an empty ALL_STAGES prints an empty table (Slice-1 default)."""
    _seed_project(tmp_path)
    result = runner.invoke(cli.app, ["--projects-root", str(tmp_path), "status", "entry"])
    assert result.exit_code == 0, result.output
    assert "entry" in result.output


# --------------------------------------------------------------------------- #
# not-yet-implemented verbs (Slice-1 default: empty registry)
# --------------------------------------------------------------------------- #


def test_unimplemented_verb_reports_not_yet_implemented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an empty registry, a stage verb prints 'not yet implemented' and exits 2.

    `01_pick` is implemented as of Slice 6, so this test pins the empty-registry
    fallback path explicitly by clearing `ALL_STAGES` (the scenario it documents:
    a verb whose stage class has not been registered yet, e.g. `enrich` before
    its slice lands)."""
    monkeypatch.setattr(_stages, "ALL_STAGES", ())
    _seed_project(tmp_path)
    result = runner.invoke(cli.app, ["--projects-root", str(tmp_path), "enrich", "entry"])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output
    assert "not yet implemented" in result.output


def test_approve_unknown_stage_id_exits_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """approve against a stage_id absent from the registry exits 1.

    `01_pick` is now registered, so this pins the unknown-stage path against an
    unregistered id (`02_enrich`, with the registry holding only `PickStage`)."""
    monkeypatch.setattr(_stages, "ALL_STAGES", (FakeStage,))
    _seed_project(tmp_path)
    result = runner.invoke(
        cli.app, ["--projects-root", str(tmp_path), "approve", "entry", "02_enrich"]
    )
    assert result.exit_code == cli._EXIT_USER_ERROR, result.output
    assert "unknown stage_id" in result.output


# --------------------------------------------------------------------------- #
# TC-23.3 — dispatcher failure path
# --------------------------------------------------------------------------- #


def test_tc_23_3_run_exception_transitions_to_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-23.3: when stage.run() raises, the stage is FAILED and the manifest is saved."""
    project = _seed_project(tmp_path)

    class BoomStage(FakeStage):
        def run(self, project: Project) -> StageResult:
            raise RuntimeError("kaboom in run")

    monkeypatch.setattr(_stages, "ALL_STAGES", (BoomStage,))
    result = runner.invoke(cli.app, ["--projects-root", str(tmp_path), "pick", "entry"])
    assert result.exit_code == cli._EXIT_STAGE_FAILURE, result.output

    m = Manifest.load(project.manifest_path)
    rec = m.stages["01_pick"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "RuntimeError"
    assert "kaboom" in rec.error.message


# --------------------------------------------------------------------------- #
# TC-19.3 / TC-19.4 — lazy client construction + MissingApiKey
# --------------------------------------------------------------------------- #


def test_tc_19_4_no_client_constructed_at_cli_startup() -> None:
    """TC-19.4: importing shipcast.cli does not construct any client / import SDKs.

    The heavy SDK absence is asserted via subprocess in test_package_imports;
    here we assert the heavy SDK module is not already loaded by importing cli.
    """
    # importing cli at module top already happened; assert no SDK leaked in-proc
    # is unreliable (other tests may have imported them), so we only check that
    # the lazy registry indirection exists and is callable with no clients built.
    # The registry grows one stage per slice; resolving it constructs NO external
    # client (clients are built lazily inside `run()`, never at import or registry
    # resolution). We assert the known stages are registered (superset check) and
    # bound to the expected classes rather than pinning the exact set, so adding a
    # stage in a later slice does not require touching this Slice-1 invariant test.
    assert callable(cli._stage_registry)
    registry = cli._stage_registry()
    assert {"01_pick", "02_enrich"} <= set(registry)
    assert registry["01_pick"] is _stages.PickStage
    assert registry["02_enrich"] is _stages.EnrichStage


def test_tc_19_3_elevenlabs_missing_key_raises_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-19.3: ElevenLabsClient with an empty key raises MissingApiKey('ELEVENLABS_API_KEY').

    ElevenLabs is the one real-key client present in Slice 1's lazy registry.
    The error message carries the key NAME only — never the (empty) value.
    """
    from pydantic import SecretStr

    from shipcast.clients import ElevenLabsClient

    with pytest.raises(MissingApiKey) as excinfo:
        ElevenLabsClient(api_key=SecretStr(""))
    assert "ELEVENLABS_API_KEY" in str(excinfo.value)


def test_tc_19_3_anthropic_uses_subscription_no_api_key() -> None:
    """TC-19.3 (reconciled): AnthropicClient uses the `claude` CLI subscription.

    It takes NO api key — it must construct with no arguments and never raise
    MissingApiKey. Auth is whatever the local `claude` CLI is signed in to.
    """
    from shipcast.clients import AnthropicClient

    client = AnthropicClient()  # no key argument
    assert repr(client) == "<AnthropicClient>"
