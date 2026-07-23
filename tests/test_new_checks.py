"""Lock-in tests for the three newly implemented checks.

- etch_compensation_margin  : yield margin of the narrowest copper vs etch floor
- silkscreen_clearance       : silk clearance to board edge and drilled holes
- layer_registration_margin  : annular-ring headroom vs a registration budget
"""

from __future__ import annotations

import math
import pathlib
import tempfile

import boards  # tests/boards.py

from pcb_dfm.geometry.primitives import Bounds, Point2D, Polygon


def _run(name):
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    with tempfile.TemporaryDirectory() as td:
        z = boards.emit_zip(boards.ARCHETYPES[name](), pathlib.Path(td), name=f"{name}.zip")
        res = run_dfm_on_gerber_zip(z, ruleset_id="default")
    return {c.check_id: c for cat in res.categories for c in cat.checks}


def _measured(check):
    m = check.metric
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


# --- etch_compensation_margin ------------------------------------------------

def test_etch_margin_flags_feature_below_process_floor():
    """thin_trace_board has a 0.05 mm trace, below the 0.075 mm etch floor, so
    the worst-case margin is negative and the check must fail."""
    c = _run("thin_trace_board")["etch_compensation_margin"]
    assert c.status == "fail"
    assert _measured(c) is not None and _measured(c) < 0.0


def test_etch_margin_passes_comfortable_board():
    """clean_two_layer's narrowest copper sits well above the etch floor."""
    c = _run("clean_two_layer")["etch_compensation_margin"]
    assert c.status == "pass"
    assert _measured(c) >= 20.0  # >= target margin percent


# --- silkscreen_clearance ----------------------------------------------------

def test_silk_edge_clearance_sign():
    from pcb_dfm.checks.impl_silkscreen_clearance import _edge_clearance

    board = Bounds(min_x=0.0, min_y=0.0, max_x=20.0, max_y=14.0)
    inside = (5.0, 6.0, 5.0, 6.0)          # 1x1 silk, >= 5 mm from every edge
    assert math.isclose(_edge_clearance(inside, board), 5.0, abs_tol=1e-9)
    over_edge = (19.5, 20.5, 5.0, 6.0)     # pokes 0.5 mm past the right edge
    assert _edge_clearance(over_edge, board) < 0.0


def test_silk_hole_clearance_sign():
    from pcb_dfm.checks.impl_silkscreen_clearance import _hole_clearance

    silk = (0.0, 1.0, 0.0, 1.0)
    # Hole centre 2 mm to the right of the silk's right edge, radius 0.5.
    assert math.isclose(_hole_clearance(silk, 3.0, 0.5, 0.5), 1.5, abs_tol=1e-9)
    # Hole centred inside the silk bbox -> negative (silk covers the rim).
    assert _hole_clearance(silk, 0.5, 0.5, 0.5) < 0.0


def test_silk_clearance_detects_silk_over_a_hole():
    """The synthetic emitter writes the copper artwork as the silkscreen layer,
    so silk sits directly on the drilled pads -- a real silk-over-hole overlap.

    This only became visible once drills were parsed with correct coordinates
    (the old pcb-tools path double-converted mm-native Excellon, putting holes
    25.4x off the board). Negative clearance == silk overlapping the hole.
    """
    c = _run("clean_two_layer")["silkscreen_clearance"]
    assert c.status == "fail"
    assert _measured(c) is not None and _measured(c) < 0.0


# --- layer_registration_margin ----------------------------------------------

def test_registration_reuses_annular_ring_geometry():
    """A 1x1 mm pad centred on a 0.4 mm hole leaves a 0.3 mm ring; the
    registration check consumes exactly this drill-edge-to-pad-edge geometry."""
    from pcb_dfm.checks.impl_min_annular_ring import (
        _min_distance_to_polygon_edges,
        _point_in_polygon,
    )

    pad = Polygon(vertices=[
        Point2D(-0.5, -0.5), Point2D(0.5, -0.5), Point2D(0.5, 0.5), Point2D(-0.5, 0.5),
    ])
    assert _point_in_polygon(0.0, 0.0, pad.vertices)
    ring = _min_distance_to_polygon_edges(0.0, 0.0, pad.vertices) - 0.2  # r = 0.4/2
    assert math.isclose(ring, 0.3, abs_tol=1e-9)


def test_registration_budget_thresholds_are_micron_scale():
    """The µm metric must plumb to a 50 µm target / 25 µm floor in mm."""
    from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset
    from pcb_dfm.checks.impl_layer_registration_margin import _thresholds

    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}["layer_registration_margin"]

    class _Ctx:
        check_def = cdef

    rec, ab = _thresholds(_Ctx())
    assert math.isclose(rec, 0.05, abs_tol=1e-9)
    assert math.isclose(ab, 0.025, abs_tol=1e-9)
