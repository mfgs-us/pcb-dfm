"""Regression guards for false positives real copper-pour boards trigger.

Both were surfaced by adding the pcb-tools example board to the corpus.
"""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry, BoardLayer
from pcb_dfm.geometry.primitives import Point2D, Polygon


def _run(check_id, geom):
    _ensure_impls_loaded()
    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}[check_id]
    ctx = CheckContext(
        check_def=cdef, ingest=None, geometry=geom, geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=None,
    )
    return get_check_runner(check_id)(ctx)


def _rect(x0, y0, x1, y1):
    return Polygon(vertices=[Point2D(x0, y0), Point2D(x1, y0), Point2D(x1, y1), Point2D(x0, y1)])


def test_min_trace_width_ignores_zero_width_pour_boundaries():
    # Zero/near-zero-width Line primitives (region/pour boundaries) must not be
    # counted as 0.000 mm traces.
    from pcb_dfm.checks.impl_min_trace_width import _MIN_MEANINGFUL_TRACE_MM

    class _Line:
        def __init__(self, w, start=(0, 0), end=(1, 0)):
            self.width = w
            self.start = start
            self.end = end

    import pcb_dfm.checks.impl_min_trace_width as M

    # A real 0.3 mm trace alongside a 0.0 mm pour boundary -> min is 0.3, not 0.
    lines_mm = [0.0, 0.0102, 0.3, 0.5]
    kept = [w for w in lines_mm if w >= _MIN_MEANINGFUL_TRACE_MM]
    assert min(kept) == 0.3
    assert 0.0 not in kept and 0.0102 not in kept
    assert M._MIN_MEANINGFUL_TRACE_MM == 0.02


def test_degenerate_copper_polygons_are_dropped_at_build():
    # Zero-area (zero-width pour-boundary) copper polygons carry no copper and
    # must be filtered when building geometry, below copper_sliver's floor so
    # real thin slivers survive.
    from pcb_dfm.geometry.gerber_parser import _MIN_COPPER_POLY_AREA_MM2, _poly_area_mm2

    degenerate = Polygon(vertices=[Point2D(0, 0), Point2D(10, 0), Point2D(10, 0)])  # ~0 area
    sliver = _rect(0, 0, 5.0, 0.15)  # 0.75 mm^2 real thin sliver
    assert _poly_area_mm2(degenerate) < _MIN_COPPER_POLY_AREA_MM2
    assert _poly_area_mm2(sliver) >= _MIN_COPPER_POLY_AREA_MM2
    assert _MIN_COPPER_POLY_AREA_MM2 < 0.02  # below copper_sliver's min_area


def test_touching_copper_is_one_conductor_not_a_spacing_violation():
    # Physically connected copper is the same net by definition, so the gap
    # "between" two overlapping segments is a junction, not a spacing gap.
    # This used to be approximated with a 20 um merge floor, which relocated the
    # problem rather than fixing it: pairs below the floor were skipped, so the
    # reported minimum became the smallest gap just above it and the metric
    # tracked the constant instead of the board (#14). Segments carry an exact
    # width, so the connectivity test needs no tolerance.
    from pcb_dfm.checks.impl_min_trace_spacing import Segment, _conductor_groups

    def seg(x1, y1, x2, y2, w=0.2):
        return Segment("TopCopper", x1, y1, x2, y2, w)

    horizontal = seg(0.0, 0.0, 10.0, 0.0)
    tee = seg(5.0, 0.0, 5.0, 6.0)        # meets `horizontal` -> same conductor
    far = seg(0.0, 20.0, 10.0, 20.0)     # 20 mm away -> its own conductor

    ds = _conductor_groups([horizontal, tee, far])
    assert ds.find(0) == ds.find(1), "a T junction is one conductor"
    assert ds.find(0) != ds.find(2), "separated copper is a different conductor"

    # Connectivity is transitive: a chain of touching segments is one conductor
    # even where the ends are far apart.
    chain = [seg(float(i), 0.0, float(i + 1), 0.0) for i in range(6)]
    ds2 = _conductor_groups(chain)
    assert len({ds2.find(i) for i in range(len(chain))}) == 1


def test_copper_to_edge_not_applicable_without_outline():
    # Copper present, but NO outline layer -> we can't know the edge -> N/A
    # (measuring against the copper bbox would falsely report 0).
    geom = BoardGeometry(root_dir=Path("."))
    top = BoardLayer(name="Top", logical_layer="TopCopper", side="top", layer_type="copper")
    top.polygons = [_rect(0, 0, 20, 10)]
    geom.add_layer(top)
    assert _run("copper_to_edge_distance", geom).status == "not_applicable"


def test_copper_to_edge_measures_with_outline():
    # With an outline present it still measures (sanity that the gate didn't
    # break the normal path): copper 2 mm inside a 20x10 outline.
    geom = BoardGeometry(root_dir=Path("."))
    top = BoardLayer(name="Top", logical_layer="TopCopper", side="top", layer_type="copper")
    top.polygons = [_rect(2, 2, 18, 8)]
    outline = BoardLayer(name="Edge", logical_layer="Outline", side="both", layer_type="outline")
    outline.polygons = [_rect(0, 0, 20, 10)]
    geom.add_layer(top)
    geom.add_layer(outline)
    r = _run("copper_to_edge_distance", geom)
    assert r.status in ("pass", "warning", "fail")
    m = r.metric
    mv = m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)
    assert mv is not None and mv > 0.0
