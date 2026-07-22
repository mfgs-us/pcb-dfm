"""tombstoning_risk: thermal-mass imbalance on two-pad passives."""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset
from pcb_dfm.checks.impl_tombstoning_risk import (
    _local_copper_fraction,
    _pad_centers,
    _passive_spacing,
)
from pcb_dfm.engine.check_runner import HEURISTIC_CHECK_IDS, get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry, BoardLayer
from pcb_dfm.geometry.primitives import Point2D, Polygon
from pcb_dfm.ingest.design_model import Component, DesignData


def _rect(x0, y0, x1, y1) -> Polygon:
    return Polygon(vertices=[
        Point2D(x0, y0), Point2D(x1, y0), Point2D(x1, y1), Point2D(x0, y1),
    ])


def test_passive_spacing_from_footprint():
    assert _passive_spacing("Resistor_SMD:R_0402_1005Metric") == 0.90
    assert _passive_spacing("Capacitor_SMD:C_0603_1608Metric") == 1.55
    assert _passive_spacing("Package_SO:SOIC-8") is None


def test_pad_centers_rotation():
    (x1, y1), (x2, y2) = _pad_centers(10.0, 5.0, 0.0, 1.0)
    assert (x1, x2) == (10.5, 9.5) and y1 == y2 == 5.0
    # 90 deg -> pads separate along y
    (a1, b1), (a2, b2) = _pad_centers(10.0, 5.0, 90.0, 1.0)
    assert abs(a1 - 10.0) < 1e-9 and abs(b1 - 5.5) < 1e-9


def test_local_copper_fraction():
    full = [_rect(0, 0, 10, 10)]
    assert _local_copper_fraction(5, 5, 0.6, full) > 0.95
    assert _local_copper_fraction(50, 50, 0.6, full) == 0.0


def _ctx(geom, dd):
    _ensure_impls_loaded()
    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}["tombstoning_risk"]
    return CheckContext(
        check_def=cdef, ingest=None, geometry=geom, geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=dd,
    )


def _run(geom, dd):
    ctx = _ctx(geom, dd)
    return get_check_runner("tombstoning_risk")(ctx)


def _measured(r):
    m = r.metric
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


def _geom_top(polys):
    geom = BoardGeometry(root_dir=Path("."))
    top = BoardLayer(name="Top", logical_layer="TopCopper", side="top", layer_type="copper")
    top.polygons = list(polys)
    geom.add_layer(top)
    return geom


def _passive_at(x, y, footprint="Resistor_SMD:R_0402_1005Metric"):
    dd = DesignData(source="test")
    dd.components = [Component(ref="R1", footprint=footprint, x_mm=x, y_mm=y, rotation_deg=0.0, side="top")]
    return dd


def test_asymmetric_copper_flags_tombstoning():
    # Pad spacing 0.9 -> pads at x=9.55 and x=10.45 (component at x=10).
    # Left pad sits over a big pour; right pad over bare board.
    geom = _geom_top([_rect(0, 0, 9.9, 10)])
    r = _run(geom, _passive_at(10.0, 5.0))
    assert r.status in ("warning", "fail")
    assert _measured(r) > 25.0
    assert r.violations and r.violations[0].location.x_mm == 10.0


def test_symmetric_copper_passes():
    # Both pads over the same large pour -> balanced.
    geom = _geom_top([_rect(0, 0, 20, 10)])
    r = _run(geom, _passive_at(10.0, 5.0))
    assert r.status == "pass"
    assert _measured(r) < 10.0


def test_no_components_is_not_applicable():
    geom = _geom_top([_rect(0, 0, 20, 10)])
    assert _run(geom, DesignData(source="test")).status == "not_applicable"


def test_no_passive_footprints_is_not_applicable():
    geom = _geom_top([_rect(0, 0, 20, 10)])
    dd = _passive_at(10.0, 5.0, footprint="Package_SO:SOIC-8")
    assert _run(geom, dd).status == "not_applicable"


def test_dnp_passive_is_excluded():
    # Same asymmetric copper as the flagged case, but the part is DNP -> skipped.
    geom = _geom_top([_rect(0, 0, 9.9, 10)])
    dd = _passive_at(10.0, 5.0)
    dd.components[0].dnp = True
    assert _run(geom, dd).status == "not_applicable"


def test_bom_confirmed_non_passive_is_excluded():
    geom = _geom_top([_rect(0, 0, 9.9, 10)])
    dd = _passive_at(10.0, 5.0)
    dd.components[0].part_class = "ic"   # BOM says it's not a passive
    assert _run(geom, dd).status == "not_applicable"


def test_uses_real_pad_positions_when_present():
    from pcb_dfm.ingest.design_model import Pad
    # Pour only on the left half; pad 1 over it, pad 2 off it -> imbalance.
    geom = _geom_top([_rect(0, 0, 5, 10)])
    dd = DesignData(source="test")
    dd.components = [Component(
        ref="R1", x_mm=5.0, y_mm=5.0, side="top", footprint="Resistor_SMD:R_0402_1005Metric",
        placed=True, pads=[Pad("1", 2.0, 5.0), Pad("2", 8.0, 5.0)],
    )]
    r = _run(geom, dd)
    assert r.status in ("warning", "fail")
    assert _measured(r) > 25.0


def test_labeled_heuristic():
    assert "tombstoning_risk" in HEURISTIC_CHECK_IDS
