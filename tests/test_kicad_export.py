"""Running the pipeline from a KiCad project via kicad-cli (#13, Tier 2).

The shipped KiCad adapter reads a .kicad_pcb for design data only; Gerbers stay
the geometry-of-record. Tier 3 -- rendering the board file natively into
BoardGeometry -- is deliberately NOT built, because poured copper only exists in
a .kicad_pcb if zones were refilled before saving. Rendering a stale file would
silently measure copper that differs from what gets fabricated.

Shelling out to KiCad's own plotter sidesteps that: it fills zones as part of
plotting and applies the project's real settings, so the existing Gerber
pipeline runs on authoritative artwork.

Coverage caveat: kicad-cli is not installed in CI, so the export itself is
exercised only when KiCad is present locally. Everything around it -- detection,
the failure mode when KiCad is absent, and the provenance recorded on the result
-- is covered unconditionally, since those are what a user hits first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcb_dfm.ingest.kicad_export import (  # noqa: E402
    GEOMETRY_SOURCE_GERBER,
    GEOMETRY_SOURCE_KICAD_CLI,
    export_gerber_zip,
    kicad_cli_path,
    looks_like_kicad_project,
)

_MINIMAL_PCB = """(kicad_pcb (version 20221018) (generator pcbnew)
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (gr_line (start 0 0) (end 20 0) (layer "Edge.Cuts") (width 0.05))
)
"""


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def test_detects_a_board_file_and_a_project_directory(tmp_path):
    board = tmp_path / "proj" / "board.kicad_pcb"
    board.parent.mkdir()
    board.write_text(_MINIMAL_PCB)

    assert looks_like_kicad_project(board)
    assert looks_like_kicad_project(board.parent)
    assert looks_like_kicad_project(board.parent / "board.kicad_pro")


def test_a_gerber_zip_is_not_mistaken_for_a_project():
    """The normal input must keep taking the normal path."""
    z = Path(__file__).resolve().parent.parent / "testdata" / "mini_board.zip"
    if not z.exists():
        pytest.skip("gerber fixture missing")
    assert not looks_like_kicad_project(z)


def test_an_empty_directory_is_not_a_project(tmp_path):
    assert not looks_like_kicad_project(tmp_path)


# --------------------------------------------------------------------------
# Behaviour without KiCad installed
# --------------------------------------------------------------------------
@pytest.mark.skipif(kicad_cli_path() is not None, reason="kicad-cli is installed")
def test_missing_kicad_cli_fails_with_actionable_guidance(tmp_path):
    """There is no lesser geometry to fall back to, so this must refuse
    clearly rather than degrade into measuring nothing."""
    board = tmp_path / "board.kicad_pcb"
    board.write_text(_MINIMAL_PCB)

    with pytest.raises(RuntimeError, match="kicad-cli not found"):
        export_gerber_zip(board)


def test_a_project_directory_without_a_board_is_rejected(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises((ValueError, RuntimeError)):
        export_gerber_zip(tmp_path / "empty")


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------
def test_a_gerber_run_is_recorded_as_gerber_sourced():
    """A normal run audits the user's own fabrication package, and must say so."""
    pytest.importorskip("gerbonara", reason="gerbonara not installed")
    z = Path(__file__).resolve().parent.parent / "testdata" / "mini_board.zip"
    if not z.exists():
        pytest.skip("gerber fixture missing")

    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    res = run_dfm_on_gerber_zip(z, ruleset_id="default")
    assert res.summary.geometry_source == GEOMETRY_SOURCE_GERBER


def test_geometry_source_constants_are_distinct():
    """A report must be able to tell an audited package from a plotted design."""
    assert GEOMETRY_SOURCE_GERBER != GEOMETRY_SOURCE_KICAD_CLI


# --------------------------------------------------------------------------
# The export itself -- only runs where KiCad is installed
# --------------------------------------------------------------------------
@pytest.mark.skipif(kicad_cli_path() is None, reason="kicad-cli not installed")
def test_export_from_a_project_records_its_provenance(tmp_path):
    """A run from a design file assesses the design, not the package the user
    sends, and the result has to carry that distinction."""
    pytest.importorskip("gerbonara", reason="gerbonara not installed")
    board = tmp_path / "board.kicad_pcb"
    board.write_text(_MINIMAL_PCB)

    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    res = run_dfm_on_gerber_zip(board, ruleset_id="default")
    assert res.summary.geometry_source == GEOMETRY_SOURCE_KICAD_CLI
    assert any("export-time" in w for w in res.warnings), (
        "the result must warn that export-time faults cannot be detected"
    )
