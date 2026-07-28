"""Routing-structure review checks (batch of 🟢 ideas)."""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry, BoardLayer
from pcb_dfm.geometry.primitives import Point2D, Polygon
from pcb_dfm.ingest.design_model import DesignData, Net, NetFeature, Via


def run(cid, dd, geometry=None):
    _ensure_impls_loaded()
    ctx = CheckContext(
        check_def=load_check_definition(cid), ingest=None,
        geometry=geometry or BoardGeometry(root_dir=Path(".")),
        geometry_cache=GeometryCache(), ruleset_id="default", design_id="t",
        gerber_zip=Path("x"), design_data=dd)
    return get_check_runner(cid)(ctx)


def _net(name, segs, width=0.2, layer="F.Cu", vias=None):
    return Net(name=name,
               features=[NetFeature(layer=layer, width_mm=width, segments=segs)],
               vias=vias or [])


def _dd(nets):
    dd = DesignData(source="test")
    dd.nets = {n.name: n for n in nets}
    return dd


# -- acute_trace_angle ------------------------------------------------------
def test_acute_angle_flagged():
    # 45-deg interior V: west-in then north-west-out.
    dd = _dd([_net("SIG", [((0, 0), (5, 0)), ((5, 0), (1, 4))])])
    r = run("acute_trace_angle", dd)
    assert r.status == "warning" and "SIG" in r.violations[0].message


def test_45_chamfer_passes():
    # A 45-deg chamfer is a 135-deg interior angle -> not acute.
    dd = _dd([_net("SIG", [((0, 0), (5, 0)), ((5, 0), (9, 4))])])
    assert run("acute_trace_angle", dd).status == "pass"


# -- self_crossing_trace ----------------------------------------------------
def test_self_crossing_flagged():
    dd = _dd([_net("SIG", [((0, 0), (4, 4)), ((0, 4), (4, 0))])])
    r = run("self_crossing_trace", dd)
    assert r.status == "warning" and "SIG" in r.violations[0].message


def test_no_self_crossing_passes():
    dd = _dd([_net("SIG", [((0, 0), (4, 0)), ((4, 0), (4, 4))])])
    assert run("self_crossing_trace", dd).status == "pass"


# -- orphan_or_redundant_via ------------------------------------------------
def test_orphan_via_flagged():
    dd = _dd([_net("SIG", [((0, 0), (5, 0))], vias=[Via(x_mm=20, y_mm=20)])])
    r = run("orphan_or_redundant_via", dd)
    assert r.status == "warning"


def test_redundant_via_flagged():
    dd = _dd([_net("SIG", [((0, 0), (5, 0))],
                   vias=[Via(x_mm=5, y_mm=0), Via(x_mm=5.02, y_mm=0)])])
    assert run("orphan_or_redundant_via", dd).status == "warning"


def test_via_at_trace_end_passes():
    dd = _dd([_net("SIG", [((0, 0), (5, 0))], vias=[Via(x_mm=5, y_mm=0)])])
    assert run("orphan_or_redundant_via", dd).status == "pass"


# -- coupled_run_length -----------------------------------------------------
def test_coupled_hs_run_flagged():
    dd = _dd([_net("/USB_DP", [((0, 0), (50, 0))]),
              _net("SIG", [((0, 0.4), (50, 0.4))])])
    r = run("coupled_run_length", dd)
    assert r.status == "warning" and "USB_DP" in r.violations[0].message


def test_coupled_far_apart_passes():
    dd = _dd([_net("/USB_DP", [((0, 0), (50, 0))]),
              _net("SIG", [((0, 3.0), (50, 3.0))])])
    assert run("coupled_run_length", dd).status == "pass"


def test_coupled_na_without_hs():
    dd = _dd([_net("A", [((0, 0), (50, 0))]), _net("B", [((0, 0.4), (50, 0.4))])])
    assert run("coupled_run_length", dd).status == "not_applicable"


# -- signal_plane_adjacency -------------------------------------------------
def _cu(logical, side, poly):
    ly = BoardLayer(name=logical, logical_layer=logical, side=side, layer_type="copper")
    ly.polygons = [poly]
    return ly


def _rect(x0, y0, x1, y1):
    return Polygon(vertices=[Point2D(x0, y0), Point2D(x1, y0), Point2D(x1, y1), Point2D(x0, y1)])


def _geo(layers):
    g = BoardGeometry(root_dir=Path("."))
    for ly in layers:
        g.add_layer(ly)
    return g


_FULL = lambda: _rect(0, 0, 20, 20)      # noqa: E731  plane (full coverage)
_SMALL = lambda: _rect(0, 0, 2, 2)       # noqa: E731  signal (sparse)


def test_plane_adjacency_flagged():
    # top(sig) / In1(sig) / In2(plane) / bottom(sig) -> top has no adjacent plane.
    geo = _geo([
        _cu("F.Cu", "top", _SMALL()), _cu("InnerCopper1", "inner", _SMALL()),
        _cu("InnerCopper2", "inner", _FULL()), _cu("B.Cu", "bottom", _SMALL()),
    ])
    r = run("signal_plane_adjacency", _dd([]), geometry=geo)
    assert r.status == "warning"


def test_plane_adjacency_passes():
    geo = _geo([
        _cu("F.Cu", "top", _SMALL()), _cu("InnerCopper1", "inner", _FULL()),
        _cu("InnerCopper2", "inner", _FULL()), _cu("B.Cu", "bottom", _SMALL()),
    ])
    assert run("signal_plane_adjacency", _dd([]), geometry=geo).status == "pass"


def test_plane_adjacency_na_two_layer():
    geo = _geo([_cu("F.Cu", "top", _SMALL()), _cu("B.Cu", "bottom", _FULL())])
    assert run("signal_plane_adjacency", _dd([]), geometry=geo).status == "not_applicable"


# -- trace_over_cutout (helper) --------------------------------------------
def test_seg_hits_cutout_geometry():
    from pcb_dfm.checks.impl_trace_over_cutout import _seg_hits_cutout
    cut = [(5, 5), (10, 5), (10, 10), (5, 10)]
    verts = [Point2D(x, y) for (x, y) in cut]
    assert _seg_hits_cutout(((0, 7), (15, 7)), cut, verts) is True   # crosses it
    assert _seg_hits_cutout(((7, 7), (8, 8)), cut, verts) is True    # inside it
    assert _seg_hits_cutout(((0, 0), (2, 0)), cut, verts) is False   # clear of it
