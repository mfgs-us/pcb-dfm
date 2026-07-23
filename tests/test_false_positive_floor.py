"""The false-positive floor: a clean board must not be scored as a bad one.

`clean_two_layer` used to score 0.0 / fail with seven checks flagged. A DFM tool
that condemns a board built to be comfortable gets ignored on first use, and
every genuine finding it produces is lost in the noise -- so this is a
correctness property in its own right, not a cosmetic one.

Four of those seven were the checks being *right* about a bad fixture (the mask
and silkscreen layers were byte-identical copies of the copper layer, so every
mask opening exactly equalled its pad and silk sat on every pad and hole). The
rest were data gaps reported as findings, and one real per-layer bug.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import boards  # tests/boards.py
import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.engine.run import run_dfm_on_gerber_zip  # noqa: E402


def _digest(board, **kw):
    tmp = Path(tempfile.mkdtemp())
    z = boards.emit_zip(board, tmp, name="b.zip", **kw)
    return boards.result_digest(run_dfm_on_gerber_zip(z, ruleset_id="default"))


def _by_id(digest):
    return {c["id"]: c for c in digest["checks"]}


# --------------------------------------------------------------------------
# The headline property
# --------------------------------------------------------------------------
def test_clean_board_does_not_fail():
    d = _digest(boards.clean_two_layer())
    failing = [c["id"] for c in d["checks"] if c["status"] == "fail"]
    assert failing == [], f"the 'comfortable' archetype must not fail: {failing}"
    assert d["overall"]["status"] != "fail"
    assert d["overall"]["score"] > 50.0


def test_known_bad_board_still_fails_for_the_right_reasons():
    """The floor must not be bought by making the engine blind."""
    checks = _by_id(_digest(boards.thin_trace_board()))
    # thin_trace_board's docstring: "a 0.05 mm trace violates min trace width".
    assert checks["min_trace_width"]["status"] == "fail"
    assert checks["min_trace_width"]["measured"] == pytest.approx(0.05, abs=1e-3)


# --------------------------------------------------------------------------
# The fixture itself has to be clean for any of this to mean anything
# --------------------------------------------------------------------------
def test_mask_and_silk_are_not_copies_of_the_copper_layer():
    import zipfile

    tmp = Path(tempfile.mkdtemp())
    z = boards.emit_zip(boards.clean_two_layer(), tmp, name="b.zip")
    with zipfile.ZipFile(z) as zf:
        copper = zf.read("board-F_Cu.gbr")
        assert zf.read("board-F_Mask.gbr") != copper, "mask must not be the copper artwork"
        assert zf.read("board-F_Silkscreen.gbr") != copper, "silk must not be the copper artwork"


def test_clean_board_has_real_mask_expansion_and_clear_silk():
    checks = _by_id(_digest(boards.clean_two_layer()))
    # Mask openings are pads grown by 50 um per side, inside the 25-100 um target.
    assert checks["solder_mask_expansion"]["status"] == "pass"
    # Silk is placed away from pads, holes and the board edge.
    for cid in ("silkscreen_clearance", "silkscreen_on_copper",
                "silkscreen_over_mask_defined_pads"):
        assert checks[cid]["status"] == "pass", f"{cid} should be clean on this fixture"


# --------------------------------------------------------------------------
# Copper density is a per-layer property
# --------------------------------------------------------------------------
def test_copper_density_balance_is_per_layer():
    """Duplicating a layer must not change the figure.

    Every copper layer used to be flattened into one density map, so a 4-layer
    board counted its copper four times over in each window: the metric scaled
    with layer count (7.2 -> 14.4 -> 28.8) for identical artwork, and density
    could saturate at 100% purely from stacking.
    """
    two = _by_id(_digest(boards.clean_two_layer()))["copper_density_balance"]
    four = _by_id(_digest(boards.four_layer_planes()))["copper_density_balance"]
    # four_layer_planes IS clean_two_layer plus two identical inner planes.
    assert four["measured"] == pytest.approx(two["measured"], abs=1e-9)


# --------------------------------------------------------------------------
# Data gaps are not findings
# --------------------------------------------------------------------------
def test_single_hole_board_has_no_drill_spacing_to_report():
    """One hole is not a 0.00 mm drill-to-drill spacing."""
    checks = _by_id(_digest(boards.thin_trace_board()))  # exactly one hole
    dds = checks["drill_to_drill_spacing"]
    assert dds["status"] == "not_applicable"
    assert dds["measured"] is None, "must not report the worst possible value for 'no data'"


def test_board_without_outline_does_not_report_copper_coverage():
    board = boards.clean_two_layer()
    board.outline = []
    checks = _by_id(_digest(board))
    assert checks["copper_thermal_area"]["status"] == "not_applicable"
    assert checks["copper_thermal_area"]["measured"] is None


def test_low_copper_coverage_is_advisory_never_a_failure():
    """Copper thieving is a panel-level fab remedy, like tooling holes.

    A sparse 2-layer signal board runs 5-25% copper; failing it for that made
    every non-plane design score zero.
    """
    checks = _by_id(_digest(boards.clean_two_layer()))
    cta = checks["copper_thermal_area"]
    assert cta["measured"] < 20.0, "fixture is genuinely sparse; the point is the status"
    assert cta["status"] != "fail"


def test_no_tooling_holes_is_normal_for_a_bare_board():
    checks = _by_id(_digest(boards.clean_two_layer()))
    assert checks["missing_tooling_holes"]["status"] == "not_applicable"
