"""return_path_interruptions: high-speed trace crossing a plane gap."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry, BoardLayer
from pcb_dfm.geometry.primitives import Point2D, Polygon
from pcb_dfm.ingest.design_model import DesignData, DiffPair, Net, NetFeature


def _rect(x0, y0, x1, y1) -> Polygon:
    return Polygon(vertices=[
        Point2D(x0, y0), Point2D(x1, y0), Point2D(x1, y1), Point2D(x0, y1),
    ])


def _geometry(bottom_planes):
    geom = BoardGeometry(root_dir=Path("."))
    top = BoardLayer(name="Top", logical_layer="TopCopper", side="top", layer_type="copper")
    top.polygons = [_rect(0, 4.95, 20, 5.05)]           # the signal trace copper
    bot = BoardLayer(name="Bot", logical_layer="BottomCopper", side="bottom", layer_type="copper")
    bot.polygons = list(bottom_planes)
    geom.add_layer(top)
    geom.add_layer(bot)
    return geom


def _design(segments_dp):
    dd = DesignData(source="test")
    dd.nets = {"USB_DP": Net(name="USB_DP", features=[
        NetFeature(layer="F.Cu", length_mm=20, width_mm=0.1, segments=segments_dp),
    ])}
    dd.diff_pairs = [DiffPair(name="USB", positive="USB_DP", negative="USB_DN")]
    return dd


def _ctx(geom, dd):
    _ensure_impls_loaded()
    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}["return_path_interruptions"]
    return CheckContext(
        check_def=cdef, ingest=None, geometry=geom, geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=dd,
    )


def _run(geom, dd):
    ctx = _ctx(geom, dd)
    return get_check_runner("return_path_interruptions")(ctx)


def _measured(r):
    m = r.metric
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


def test_flags_trace_crossing_plane_slot():
    # Two plane islands with a 0.2 mm slot at x~10; the trace crosses it.
    geom = _geometry([_rect(0, 0, 9.9, 10), _rect(10.1, 0, 20, 10)])
    r = _run(geom, _design([((0, 5), (20, 5))]))
    assert r.status == "warning"                      # any crossing warns (< 10 mm)
    assert math.isclose(_measured(r), 0.2, abs_tol=0.05)
    assert r.violations and r.violations[0].location.net == "USB_DP"
    assert "gap" in r.violations[0].message


def test_solid_plane_passes():
    geom = _geometry([_rect(0, 0, 20, 10)])           # unbroken reference plane
    r = _run(geom, _design([((0, 5), (20, 5))]))
    assert r.status == "pass"
    assert _measured(r) == 0.0


def test_trace_mostly_off_plane_is_not_applicable():
    # Plane only under the first quarter of the trace -> not confidently the
    # reference; the conservative gate declines to judge rather than false-flag.
    geom = _geometry([_rect(0, 0, 5, 10)])
    r = _run(geom, _design([((0, 5), (20, 5))]))
    assert r.status == "not_applicable"


def test_no_design_data_is_not_applicable():
    geom = _geometry([_rect(0, 0, 20, 10)])
    ctx = _ctx(geom, None)
    r = get_check_runner("return_path_interruptions")(ctx)
    assert r.status == "not_applicable"


def test_no_high_speed_nets_is_not_applicable():
    geom = _geometry([_rect(0, 0, 20, 10)])
    dd = DesignData(source="test")
    dd.nets = {"GND": Net(name="GND", features=[
        NetFeature(layer="F.Cu", length_mm=20, segments=[((0, 5), (20, 5))]),
    ])}
    dd.diff_pairs = []                                 # nothing marks a net high-speed
    r = _run(geom, dd)
    assert r.status == "not_applicable"


def test_registered_in_ruleset():
    _ensure_impls_loaded()
    ids = {c.id for c in load_check_definitions_for_ruleset("default")}
    assert "return_path_interruptions" in ids
    # sanity: runner resolves
    assert get_check_runner("return_path_interruptions") is not None


@pytest.mark.parametrize("bad", [[]])
def test_no_planes_is_not_applicable(bad):
    # Bottom layer present but with no plane-sized copper -> no reference plane.
    geom = _geometry([_rect(0, 0, 1, 1)])              # 1 mm^2, below plane threshold
    r = _run(geom, _design([((0, 5), (20, 5))]))
    assert r.status == "not_applicable"
