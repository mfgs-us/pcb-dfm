"""Net-tagged geometry: correlate design-data nets with copper polygons."""

from __future__ import annotations

import math
from pathlib import Path

from pcb_dfm.geometry.layer_model import BoardGeometry, BoardLayer
from pcb_dfm.geometry.net_map import build_net_map, get_or_build_net_map
from pcb_dfm.geometry.primitives import Point2D, Polygon
from pcb_dfm.ingest.design_model import DesignData, DiffPair, Net, NetFeature


def _rect(x0, y0, x1, y1) -> Polygon:
    return Polygon(vertices=[
        Point2D(x0, y0), Point2D(x1, y0), Point2D(x1, y1), Point2D(x0, y1),
    ])


def _geometry():
    """Two nets, two copper polygons each, running parallel on the top layer.

    Positive copper occupies y in [0, 0.1]; negative y in [0.3, 0.4] -> a true
    copper edge-to-edge gap of 0.2 mm. (Centrelines are 0.3 mm apart, so the
    edge measure is distinguishable from a centreline measure.)
    """
    geom = BoardGeometry(root_dir=Path("."))
    top = BoardLayer(name="Top", logical_layer="TopCopper", side="top", layer_type="copper")
    top.polygons = [
        _rect(0, 0.0, 10, 0.1), _rect(10, 0.0, 20, 0.1),      # USB_DP
        _rect(0, 0.3, 10, 0.4), _rect(10, 0.3, 20, 0.4),      # USB_DN
    ]
    geom.add_layer(top)
    return geom, top


def _design(layer="F.Cu"):
    dd = DesignData(source="test")
    dp = Net(name="USB_DP", features=[
        NetFeature(layer=layer, length_mm=10, width_mm=0.1, segments=[((0, 0.05), (10, 0.05))]),
        NetFeature(layer=layer, length_mm=10, width_mm=0.1, segments=[((10, 0.05), (20, 0.05))]),
    ])
    dn = Net(name="USB_DN", features=[
        NetFeature(layer=layer, length_mm=10, width_mm=0.1, segments=[((0, 0.35), (10, 0.35))]),
        NetFeature(layer=layer, length_mm=10, width_mm=0.1, segments=[((10, 0.35), (20, 0.35))]),
    ])
    dd.nets = {"USB_DP": dp, "USB_DN": dn}
    dd.diff_pairs = [DiffPair(name="USB", positive="USB_DP", negative="USB_DN")]
    return dd


def test_tags_each_polygon_with_its_net():
    geom, top = _geometry()
    nm = build_net_map(geom, _design())
    assert nm is not None
    # The two y~0.05 polygons are USB_DP; the two y~0.35 polygons are USB_DN.
    assert nm.net_of(top.polygons[0]) == "USB_DP"
    assert nm.net_of(top.polygons[1]) == "USB_DP"
    assert nm.net_of(top.polygons[2]) == "USB_DN"
    assert nm.net_of(top.polygons[3]) == "USB_DN"
    assert nm.tagged_polygon_count() == 4
    assert nm.nets() == ["USB_DN", "USB_DP"]


def test_layer_canonicalization_maps_fcu_to_topcopper():
    # Design-data layer "F.Cu" must correlate against geometry "TopCopper".
    geom, _ = _geometry()
    nm = build_net_map(geom, _design(layer="F.Cu"))
    assert nm is not None
    assert len(nm.polygons_for_net("USB_DP")) == 2
    assert all(layer == "TopCopper" for layer, _p in nm.polygons_for_net("USB_DP"))


def test_min_spacing_is_true_copper_edge_gap():
    geom, _ = _geometry()
    nm = build_net_map(geom, _design())
    gap = nm.min_spacing_between_nets("USB_DP", "USB_DN", max_gap_mm=1.0)
    # Copper edges are 0.2 mm apart (not the 0.3 mm centreline distance).
    assert gap is not None and math.isclose(gap, 0.2, abs_tol=1e-9)


def test_coupled_gaps_respect_max_coupling():
    geom, _ = _geometry()
    nm = build_net_map(geom, _design())
    assert len(nm.coupled_edge_gaps("USB_DP", "USB_DN", 1.0)) == 2
    # A coupling window tighter than the real gap couples nothing.
    assert nm.coupled_edge_gaps("USB_DP", "USB_DN", 0.1) == []


def test_none_without_design_data_or_geometry():
    geom, _ = _geometry()
    assert build_net_map(geom, None) is None
    empty = DesignData(source="test")            # nets with no routed geometry
    empty.nets = {"X": Net(name="X")}
    assert build_net_map(geom, empty) is None


def test_diff_pair_spacing_uses_tagged_copper():
    """End-to-end: the check consumes the net map (cache is populated) and
    produces a real result instead of not_applicable."""
    from pcb_dfm.checks import _ensure_impls_loaded
    from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset
    from pcb_dfm.engine.check_runner import get_check_runner
    from pcb_dfm.engine.context import CheckContext
    from pcb_dfm.engine.geometry_cache import GeometryCache

    _ensure_impls_loaded()
    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}["diff_pair_spacing"]
    geom, _ = _geometry()
    cache = GeometryCache()
    ctx = CheckContext(
        check_def=cdef, ingest=None, geometry=geom, geometry_cache=cache,
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=_design(),
    )
    result = get_check_runner("diff_pair_spacing")(ctx)
    assert result.status == "pass"                     # constant 0.2 mm gap -> no variation
    assert cache.has(cache.key("net_map"))             # the net map was built + cached
    assert get_or_build_net_map(ctx).tagged_polygon_count() == 4
