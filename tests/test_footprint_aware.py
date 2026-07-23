"""Footprint data restores hard-fail to the pad-centric checks.

Three checks were made advisory because, from artwork alone, "a pad" is only an
area/aspect guess -- one that also admits trace stubs, pour fingers and a via's
own landing ring. Measuring mask expansion or silk coverage against those
produced findings on copper that is not a pad at all.

Placement data (KiCad footprints, or any source that fills DesignData.components)
states where each pad actually is, so a copper polygon containing that point IS
that pad. With it these checks measure the right copper and may fail again.

Each test below is a pair: identical board and identical measured value, failing
only once footprint data is supplied. That is the point -- the geometry did not
change, the confidence did.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import boards  # tests/boards.py
import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.ingest.design_model import Component, DesignData, Pad  # noqa: E402


def _run(board, design_data):
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    with tempfile.TemporaryDirectory() as td:
        z = boards.emit_zip(board, Path(td), name="b.zip")
        res = run_dfm_on_gerber_zip(z, ruleset_id="default", design_data=design_data)
    return {c.check_id: c for cat in res.categories for c in cat.checks}


def _measured(c):
    m = c.metric
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


def _footprint_for(*xy, side="top"):
    """Placement data whose pads sit at the given board coordinates."""
    return DesignData(components=[Component(
        ref="U1", side=side, placed=True,
        pads=[Pad(str(i + 1), x, y, through_hole=True) for i, (x, y) in enumerate(xy)],
    )])


# clean_two_layer places 1.2 mm pads at (4, 10) and (14, 10).
_PADS = ((4.0, 10.0), (14.0, 10.0))


# --------------------------------------------------------------------------
# The map itself
# --------------------------------------------------------------------------
def test_pad_map_identifies_only_real_component_pads():
    from pcb_dfm.engine.run import build_geometry_for
    from pcb_dfm.geometry.pad_map import build_pad_map

    board = boards.clean_two_layer()
    with tempfile.TemporaryDirectory() as td:
        z = boards.emit_zip(board, Path(td), name="b.zip")
        geom = build_geometry_for(z)

        pm = build_pad_map(geom, _footprint_for(*_PADS))
        assert pm is not None
        assert pm.pad_polygon_count() > 0
        assert pm.components() == ["U1"]

        # Without placement data there is nothing to identify pads with, and the
        # checks must fall back rather than treat the board as pad-free.
        assert build_pad_map(geom, None) is None


def test_pad_map_does_not_claim_traces_as_pads():
    """A pad is one specific polygon; the trace leaving it is not part of it."""
    from pcb_dfm.engine.run import build_geometry_for
    from pcb_dfm.geometry.pad_map import build_pad_map

    board = boards.clean_two_layer()   # also carries two 0.3 mm traces
    with tempfile.TemporaryDirectory() as td:
        z = boards.emit_zip(board, Path(td), name="b.zip")
        geom = build_geometry_for(z)
        pm = build_pad_map(geom, _footprint_for(*_PADS))

    total = sum(len(lyr.polygons) for lyr in geom.get_layers_by_type("copper"))
    assert pm.pad_polygon_count() < total, "traces must not be labelled as pads"


# --------------------------------------------------------------------------
# via_in_pad_thermal_balance
# --------------------------------------------------------------------------
def _via_in_pad_board():
    # A 0.5 mm via (inside the 0.15-0.60 mm via-like band) in a 0.55 mm pad:
    # area ratio = pi*0.25^2 / 0.55^2 ~= 65%, past the 40% absolute maximum.
    return boards.Board(
        outline=[(0, 0), (20, 0), (20, 14), (0, 14)],
        pads=[boards.Pad(10, 7, 0.55, 0.55)],
        holes=[boards.Hole(10, 7, 0.5)],
    )


def test_via_in_pad_fails_only_with_footprint_data():
    expected = 100.0 * math.pi * 0.25 ** 2 / 0.55 ** 2

    without = _run(_via_in_pad_board(), None)["via_in_pad_thermal_balance"]
    with_fp = _run(_via_in_pad_board(), _footprint_for((10.0, 7.0)))["via_in_pad_thermal_balance"]

    assert _measured(without) == pytest.approx(expected, abs=1.0)
    assert _measured(with_fp) == pytest.approx(expected, abs=1.0), "same geometry"
    assert without.status == "warning", "artwork alone cannot confirm a component pad"
    assert with_fp.status == "fail", "footprint data confirms it; the check may fail again"


# --------------------------------------------------------------------------
# solder_mask_expansion
# --------------------------------------------------------------------------
def test_mask_on_pad_fails_only_with_footprint_data():
    def board():
        b = boards.clean_two_layer()
        b.mask_expansion_mm = -0.20     # opening 0.4 mm smaller than the pad
        return b

    without = _run(board(), None)["solder_mask_expansion"]
    with_fp = _run(board(), _footprint_for(*_PADS))["solder_mask_expansion"]

    assert _measured(without) == pytest.approx(_measured(with_fp)), "same geometry"
    assert without.status == "warning"
    assert with_fp.status == "fail"


def test_healthy_mask_expansion_still_passes_with_footprint_data():
    """Footprint data must not manufacture failures on a good board."""
    c = _run(boards.clean_two_layer(), _footprint_for(*_PADS))["solder_mask_expansion"]
    assert c.status == "pass"


# --------------------------------------------------------------------------
# silkscreen_over_mask_defined_pads
# --------------------------------------------------------------------------
def test_silk_on_pad_fails_only_with_footprint_data():
    def board():
        b = boards.clean_two_layer()
        b.silk = [boards.Trace(13.0, 10.0, 15.0, 10.0, 0.6)]   # across the (14,10) pad
        return b

    without = _run(board(), None)["silkscreen_over_mask_defined_pads"]
    with_fp = _run(board(), _footprint_for(*_PADS))["silkscreen_over_mask_defined_pads"]

    assert _measured(without) == pytest.approx(_measured(with_fp)), "same geometry"
    assert without.status == "warning"
    assert with_fp.status == "fail"


def test_silk_clear_of_pads_passes_with_footprint_data():
    board = boards.clean_two_layer()
    board.silk = [boards.Trace(9.0, 2.0, 11.0, 2.0, 0.2)]      # nowhere near a pad
    c = _run(board, _footprint_for(*_PADS))["silkscreen_over_mask_defined_pads"]
    assert c.status == "pass"
