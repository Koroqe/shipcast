"""Integration tests for the `s03_brand` stage + the `brand` CLI verb (Slice 10).

Owned TCs (Section 6 + Section 20):
- TC-6.1:  complete pack, no hint → proposal.json + logo.png + style_sheet.png +
           voice.md; valid PNG headers; schema-valid proposal.
- TC-6.2..6.5: missing voice.md / fonts / logo / all → BrandPackIncomplete listing
           the file(s), NO API call (mock clients explode if reached).
- TC-6.6:  palette.hint.json present → extract_css_palette NEVER called.
- TC-6.7:  brand-pack style_sheet.png → generate_image NEVER called.
- TC-6.8:  logo None → 1x1 transparent PNG + logo_detected=false.
- TC-6.10: RFC1918-resolving live_url → ValidationError BEFORE any playwright call.
- TC-6.11: edit proposal.json then approve → manually_edited=true + changed list.
- TC-6.12: approve without edits → manually_edited=false.
- TC-6.13: config_snapshot byte-identical before/after.
- TC-6.14: replace all outputs → all listed; manually_edited=true.
- TC-6.15: byte-identical replacement → no false manually_edited.
- TC-6.16/TC-20.1: voice.md copied to 03_brand/voice.md, in outputs, bytes match.
- TC-20.2: removing 03_brand/voice.md makes s04_plan.check_inputs raise.

All external clients are injected via a mock `clients_factory`. `live_url` DNS
(`socket.getaddrinfo`) is monkeypatched to a public IP for accept cases. No real
API / network / browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

import shipcast.cli as cli
import shipcast.schemas as schemas
import shipcast.stages.s03_brand as brand_mod
from shipcast.manifest import Manifest, StageStatus
from shipcast.schemas import BrandProposal

runner = CliRunner()

_REPO_FIXTURES = (
    Path(__file__).resolve().parent.parent / "fixtures" / "repos" / "getdeal_min"
)
_CHANGELOG = (_REPO_FIXTURES / "CHANGELOG.md").read_text(encoding="utf-8")

# A real, openable 1x1 PNG produced by Pillow (valid header).
from shipcast.brand.extractor import transparent_1x1_png  # noqa: E402

REAL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

SLUG = "getdeal-platform-monorepo--add-csv-export"
BRAND_SLUG = "test-brand"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repos_root"
    root.mkdir()
    monkeypatch.setattr(schemas, "ALLOWED_REPO_ROOT", root)
    return root


@pytest.fixture
def target_repo(repo_root: Path) -> Path:
    repo = repo_root / "getdeal-platform-monorepo"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")
    return repo


def _root(projects_root: Path) -> list[str]:
    return ["--projects-root", str(projects_root)]


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    import socket

    monkeypatch.setattr(socket, "getaddrinfo", _fake)


def _private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [(2, 1, 6, "", ("192.168.1.1", 0))]

    import socket

    monkeypatch.setattr(socket, "getaddrinfo", _fake)


def _seed_brand_pack(
    projects_root: Path,
    *,
    voice: bool = True,
    fonts: bool = True,
    logo: str | None = "svg",
    palette_hint: dict[str, str] | None = None,
    style_sheet: bool = False,
) -> Path:
    root = projects_root / "_brand" / BRAND_SLUG
    (root / "fonts").mkdir(parents=True, exist_ok=True)
    if voice:
        (root / "voice.md").write_text(
            "# Voice\ncaption_mode: chip\n", encoding="utf-8"
        )
    if fonts:
        (root / "fonts" / "Inter.ttf").write_bytes(b"TTF-BYTES")
    if logo == "svg":
        (root / "logo.svg").write_text("<svg/>", encoding="utf-8")
    elif logo == "png":
        (root / "logo.png").write_bytes(REAL_PNG)
    if palette_hint is not None:
        (root / "palette.hint.json").write_text(
            json.dumps(palette_hint), encoding="utf-8"
        )
    if style_sheet:
        (root / "style_sheet.png").write_bytes(REAL_PNG)
    return root


def _pick_enrich_approve(
    projects_root: Path,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    live_url: str | None,
) -> None:
    """Run pick → enrich → approve both, so s03_brand's gate is satisfied.

    `enrich` is driven with mocked Gemini + a no-op subprocess; the `live_url`
    is written into input.yaml so brand can read it.
    """
    import subprocess

    import shipcast.stages.s02_enrich as enrich_mod

    # pick (create mode)
    result = runner.invoke(
        cli.app,
        [*_root(projects_root), "pick", str(target_repo), "--entry", "Add CSV export"],
    )
    assert result.exit_code == 0, result.output

    # write input.yaml with brand_slug + optional live_url
    input_path = projects_root / SLUG / "input.yaml"
    data: dict[str, Any] = {
        "repo_path": str(target_repo),
        "entry_heading": "Add CSV export",
        "brand_slug": BRAND_SLUG,
        "video_mode": "standard",
    }
    if live_url is not None:
        data["live_url"] = live_url
    input_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "01_pick"])
    assert result.exit_code == 0, result.output

    # enrich with mocks (no live_url effect needed — force None to skip playwright)
    enrich_input = dict(data)
    enrich_input.pop("live_url", None)
    input_path.write_text(yaml.safe_dump(enrich_input), encoding="utf-8")

    gemini = MagicMock()
    gemini.multimodal.return_value = "A compelling marketing narrative."

    def _enrich_factory(project: Any) -> Any:
        class _B:
            def __init__(self) -> None:
                self.gemini = gemini
                self.playwright = None

        return _B()

    monkeypatch.setattr(enrich_mod, "_default_clients_factory", _enrich_factory)

    def _fake_run(cmd: list[str], *a: Any, **k: Any) -> Any:
        if cmd[0] in ("gh", "git"):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "claude":
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd!r}")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = runner.invoke(cli.app, [*_root(projects_root), "enrich", SLUG])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "02_enrich"])
    assert result.exit_code == 0, result.output

    # restore the real input.yaml (with live_url) for the brand stage
    input_path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _install_brand_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gemini: MagicMock,
    playwright: MagicMock,
) -> None:
    def _factory(project: Any) -> Any:
        class _B:
            def __init__(self) -> None:
                self.gemini = gemini
                self.playwright = playwright

        return _B()

    monkeypatch.setattr(brand_mod, "_default_clients_factory", _factory)


def _explode_clients(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    """Clients whose every method raises — proves they are never reached."""
    gemini = MagicMock()
    gemini.generate_image.side_effect = AssertionError("gemini called")
    playwright = MagicMock()
    playwright.extract_css_palette.side_effect = AssertionError("palette called")
    playwright.extract_font_family.side_effect = AssertionError("font called")
    playwright.screenshot_logo.side_effect = AssertionError("logo called")
    _install_brand_clients(monkeypatch, gemini=gemini, playwright=playwright)
    return gemini, playwright


def _happy_clients(
    monkeypatch: pytest.MonkeyPatch, *, logo: bytes | None = REAL_PNG
) -> tuple[MagicMock, MagicMock]:
    gemini = MagicMock()
    gemini.generate_image.return_value = REAL_PNG
    playwright = MagicMock()
    playwright.extract_css_palette.return_value = [
        "#112233",
        "#445566",
        "#778899",
        "#aabbcc",
        "#ddeeff",
    ]
    playwright.extract_font_family.return_value = "Inter, sans-serif"
    playwright.screenshot_logo.return_value = logo
    _install_brand_clients(monkeypatch, gemini=gemini, playwright=playwright)
    return gemini, playwright


# --------------------------------------------------------------------------- #
# TC-6.1 — happy path (Playwright extraction)
# --------------------------------------------------------------------------- #


def test_tc_6_1_happy_path(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_brand_pack(projects_root)
    _public_dns(monkeypatch)
    _pick_enrich_approve(
        projects_root, target_repo, monkeypatch, live_url="https://example.com"
    )
    gemini, playwright = _happy_clients(monkeypatch)

    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code == 0, result.output

    bdir = projects_root / SLUG / "03_brand"
    proposal_path = bdir / "proposal.json"
    logo_path = bdir / "logo.png"
    sheet_path = bdir / "style_sheet.png"
    voice_path = bdir / "voice.md"
    assert proposal_path.is_file()
    assert logo_path.read_bytes()[:4] == b"\x89PNG"
    assert sheet_path.read_bytes()[:4] == b"\x89PNG"
    assert voice_path.is_file()

    proposal = BrandProposal.model_validate_json(proposal_path.read_text())
    assert proposal.font_family == "Inter, sans-serif"
    assert proposal.logo_detected is True
    assert len(proposal.palette) == 5

    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["03_brand"]
    assert rec.status == StageStatus.DONE
    assert set(rec.outputs) == {
        "03_brand/proposal.json",
        "03_brand/logo.png",
        "03_brand/style_sheet.png",
        "03_brand/voice.md",
    }
    gemini.generate_image.assert_called_once()
    assert gemini.generate_image.call_args.kwargs["aspect_ratio"] == "1:1"
    playwright.extract_css_palette.assert_called_once()


# --------------------------------------------------------------------------- #
# TC-6.2..6.5 — BrandPackIncomplete BEFORE any client call
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"voice": False}, "voice.md"),
        ({"fonts": False}, "ttf"),
        ({"logo": None}, "logo"),
    ],
)
def test_tc_6_2_to_6_4_brand_pack_incomplete(
    projects_root: Path,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    needle: str,
) -> None:
    _seed_brand_pack(projects_root, **kwargs)
    _pick_enrich_approve(
        projects_root, target_repo, monkeypatch, live_url="https://example.com"
    )
    gemini, playwright = _explode_clients(monkeypatch)

    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code != 0
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    rec = m.stages["03_brand"]
    assert rec.status == StageStatus.FAILED
    assert rec.error is not None
    assert rec.error.type == "BrandPackIncomplete"
    assert needle.lower() in rec.error.message.lower()
    # No client was ever constructed/called.
    gemini.generate_image.assert_not_called()
    playwright.extract_css_palette.assert_not_called()


def test_tc_6_5_empty_pack_lists_all_three(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (projects_root / "_brand" / BRAND_SLUG).mkdir(parents=True)
    _pick_enrich_approve(
        projects_root, target_repo, monkeypatch, live_url="https://example.com"
    )
    _explode_clients(monkeypatch)
    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code != 0
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    msg = m.stages["03_brand"].error.message  # type: ignore[union-attr]
    assert "voice.md" in msg and "ttf" in msg.lower() and "logo" in msg.lower()


# --------------------------------------------------------------------------- #
# TC-6.6 — palette.hint.json SKIPS Playwright entirely
# --------------------------------------------------------------------------- #


def test_tc_6_6_palette_hint_skips_playwright(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hint = {"primary": "#FF0000", "accent": "#00FF00", "neutral": "#0000FF"}
    _seed_brand_pack(projects_root, palette_hint=hint, logo="png")
    # No live_url at all — proves the hint path needs no network.
    _pick_enrich_approve(projects_root, target_repo, monkeypatch, live_url=None)

    gemini = MagicMock()
    gemini.generate_image.return_value = REAL_PNG
    playwright = MagicMock()
    playwright.extract_css_palette.side_effect = AssertionError("palette called!")
    playwright.extract_font_family.side_effect = AssertionError("font called!")
    playwright.screenshot_logo.side_effect = AssertionError("logo called!")
    _install_brand_clients(monkeypatch, gemini=gemini, playwright=playwright)

    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code == 0, result.output

    proposal = BrandProposal.model_validate_json(
        (projects_root / SLUG / "03_brand" / "proposal.json").read_text()
    )
    assert proposal.palette == ["#FF0000", "#00FF00", "#0000FF"]
    playwright.extract_css_palette.assert_not_called()
    playwright.screenshot_logo.assert_not_called()


# --------------------------------------------------------------------------- #
# TC-6.7 — operator-supplied style_sheet.png SKIPS Gemini
# --------------------------------------------------------------------------- #


def test_tc_6_7_style_sheet_skips_gemini(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_brand_pack(projects_root, style_sheet=True)
    _public_dns(monkeypatch)
    _pick_enrich_approve(
        projects_root, target_repo, monkeypatch, live_url="https://example.com"
    )
    gemini = MagicMock()
    gemini.generate_image.side_effect = AssertionError("gemini called!")
    playwright = MagicMock()
    playwright.extract_css_palette.return_value = ["#112233"]
    playwright.extract_font_family.return_value = "Inter"
    playwright.screenshot_logo.return_value = REAL_PNG
    _install_brand_clients(monkeypatch, gemini=gemini, playwright=playwright)

    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code == 0, result.output
    gemini.generate_image.assert_not_called()
    assert (projects_root / SLUG / "03_brand" / "style_sheet.png").read_bytes()[:4] == b"\x89PNG"


# --------------------------------------------------------------------------- #
# TC-6.8 — logo None → 1x1 transparent PNG, logo_detected=false
# --------------------------------------------------------------------------- #


def test_tc_6_8_no_logo_transparent_placeholder(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_brand_pack(projects_root)
    _public_dns(monkeypatch)
    _pick_enrich_approve(
        projects_root, target_repo, monkeypatch, live_url="https://example.com"
    )
    _happy_clients(monkeypatch, logo=None)

    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code == 0, result.output

    proposal = BrandProposal.model_validate_json(
        (projects_root / SLUG / "03_brand" / "proposal.json").read_text()
    )
    assert proposal.logo_detected is False
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO((projects_root / SLUG / "03_brand" / "logo.png").read_bytes()))
    assert img.size == (1, 1)
    assert img.getpixel((0, 0)) == (0, 0, 0, 0)


# --------------------------------------------------------------------------- #
# TC-6.10 — RFC1918 live_url → ValidationError BEFORE any playwright call
# --------------------------------------------------------------------------- #


def test_tc_6_10_private_url_rejected_before_playwright(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_brand_pack(projects_root)
    # input.yaml with a private-resolving live_url. pick/enrich need it to NOT
    # be validated as private at their parse time, so we resolve public during
    # setup, then flip to private for the brand run.
    _public_dns(monkeypatch)
    _pick_enrich_approve(
        projects_root, target_repo, monkeypatch, live_url="https://internal.example.com"
    )
    _private_dns(monkeypatch)
    _, playwright = _explode_clients(monkeypatch)

    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code != 0
    playwright.extract_css_palette.assert_not_called()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["03_brand"].status == StageStatus.FAILED


# --------------------------------------------------------------------------- #
# TC-6.13 — config_snapshot byte-identical before/after
# --------------------------------------------------------------------------- #


def test_tc_6_13_config_snapshot_unchanged(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_brand_pack(projects_root)
    _public_dns(monkeypatch)
    _pick_enrich_approve(
        projects_root, target_repo, monkeypatch, live_url="https://example.com"
    )
    before = json.dumps(
        Manifest.load(projects_root / SLUG / "manifest.json").config_snapshot,
        sort_keys=True,
    )
    _happy_clients(monkeypatch)
    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code == 0, result.output
    after = json.dumps(
        Manifest.load(projects_root / SLUG / "manifest.json").config_snapshot,
        sort_keys=True,
    )
    assert before == after
    # And no brand data leaked into it. config_snapshot legitimately carries
    # public Settings (voice_id, models, durations); what must NOT appear is any
    # brand-EXTRACTION artifact: the palette, font_family, or logo flag.
    assert "palette" not in after
    assert "font_family" not in after
    assert "logo_detected" not in after


# --------------------------------------------------------------------------- #
# TC-6.16 / TC-20.1 — voice.md copied as declared output; bytes match
# --------------------------------------------------------------------------- #


def test_tc_6_16_voice_md_copied_as_declared_output(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack_root = _seed_brand_pack(projects_root)
    _public_dns(monkeypatch)
    _pick_enrich_approve(
        projects_root, target_repo, monkeypatch, live_url="https://example.com"
    )
    _happy_clients(monkeypatch)
    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code == 0, result.output

    copied = projects_root / SLUG / "03_brand" / "voice.md"
    assert copied.is_file()
    assert copied.read_bytes() == (pack_root / "voice.md").read_bytes()
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert "03_brand/voice.md" in m.stages["03_brand"].outputs


# --------------------------------------------------------------------------- #
# TC-6.11 / TC-6.12 / TC-6.14 / TC-6.15 — manually_edited via approve
# --------------------------------------------------------------------------- #


def _run_brand_done(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_brand_pack(projects_root)
    _public_dns(monkeypatch)
    _pick_enrich_approve(
        projects_root, target_repo, monkeypatch, live_url="https://example.com"
    )
    _happy_clients(monkeypatch)
    result = runner.invoke(cli.app, [*_root(projects_root), "brand", SLUG])
    assert result.exit_code == 0, result.output


def test_tc_6_12_approve_without_edits_not_flagged(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_brand_done(projects_root, target_repo, monkeypatch)
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "03_brand"])
    assert result.exit_code == 0, result.output
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["03_brand"].manually_edited is False


def test_tc_6_11_edit_proposal_then_approve_flags(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_brand_done(projects_root, target_repo, monkeypatch)
    proposal_path = projects_root / SLUG / "03_brand" / "proposal.json"
    data = json.loads(proposal_path.read_text())
    data["palette"] = ["#000000"]  # operator trims to a different palette
    proposal_path.write_text(json.dumps(data), encoding="utf-8")

    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "03_brand"])
    assert result.exit_code == 0, result.output
    assert "Manual edits detected" in result.output
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["03_brand"].manually_edited is True


def test_tc_6_14_replace_all_outputs_flags(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_brand_done(projects_root, target_repo, monkeypatch)
    bdir = projects_root / SLUG / "03_brand"
    (bdir / "proposal.json").write_text(
        '{"palette": ["#abcdef"], "font_family": "X", "logo_detected": true}',
        encoding="utf-8",
    )
    (bdir / "logo.png").write_bytes(REAL_PNG + b"\x00")
    (bdir / "style_sheet.png").write_bytes(REAL_PNG + b"\x00")
    (bdir / "voice.md").write_text("edited voice\n", encoding="utf-8")

    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "03_brand"])
    assert result.exit_code == 0, result.output
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["03_brand"].manually_edited is True
    # All four files surface in the changed-file list.
    for name in ("proposal.json", "logo.png", "style_sheet.png", "voice.md"):
        assert name in result.output


def test_tc_6_15_byte_identical_replacement_not_flagged(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_brand_done(projects_root, target_repo, monkeypatch)
    logo_path = projects_root / SLUG / "03_brand" / "logo.png"
    same = logo_path.read_bytes()
    logo_path.write_bytes(same)  # rewrite identical bytes (mtime bump)
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "03_brand"])
    assert result.exit_code == 0, result.output
    m = Manifest.load(projects_root / SLUG / "manifest.json")
    assert m.stages["03_brand"].manually_edited is False


def test_tc_20_2_downstream_check_inputs_catches_missing_voice_md(
    projects_root: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-20.2: removing 03_brand/voice.md makes a downstream gate raise.

    s04_plan lands in Slice 11; the Finding-1 mechanism is the generic
    ``BaseStage.check_inputs`` upstream-output existence check. Because
    ``s03_brand`` DECLARES ``03_brand/voice.md`` as an output, ANY stage that
    ``requires=("03_brand",)`` (s04_plan/s08_video/s10_copy) raises
    ``StageInputMissing`` when that file is removed. We prove it here with a
    synthetic downstream stage using the unmodified default ``check_inputs``.
    """
    from typing import ClassVar

    from shipcast.errors import StageInputMissing
    from shipcast.schemas import BrandProposal as _BP
    from shipcast.stages._base import BaseStage

    _run_brand_done(projects_root, target_repo, monkeypatch)
    # Approve 03_brand so the downstream gate's done+approved check passes and we
    # isolate the missing-output failure.
    result = runner.invoke(cli.app, [*_root(projects_root), "approve", SLUG, "03_brand"])
    assert result.exit_code == 0, result.output

    class _DownstreamStage(BaseStage):
        id: ClassVar[str] = "04_plan"
        requires: ClassVar[tuple[str, ...]] = ("03_brand",)
        output_schema: ClassVar[type[_BP]] = _BP
        review_checklist_items: ClassVar[tuple[str, ...]] = ("x",)

    from shipcast.project import Project

    project = Project.load(projects_root, SLUG)
    # Sanity: with voice.md present the gate passes.
    _DownstreamStage().check_inputs(project)

    (projects_root / SLUG / "03_brand" / "voice.md").unlink()
    project = Project.load(projects_root, SLUG)
    with pytest.raises(StageInputMissing, match=r"voice\.md"):
        _DownstreamStage().check_inputs(project)


def test_imports_no_playwright() -> None:
    """transparent_1x1_png is importable without pulling playwright (used above)."""
    assert transparent_1x1_png().startswith(b"\x89PNG")
