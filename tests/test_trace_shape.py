"""Trace-shape review checks -- right-angle bends, necking, meander, via count."""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import DesignData, Net, NetFeature, Via


def run(cid, dd):
    _ensure_impls_loaded()
    ctx = CheckContext(
        check_def=load_check_definition(cid), ingest=None,
        geometry=BoardGeometry(root_dir=Path(".")), geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=dd)
    return get_check_runner(cid)(ctx)


def _net(name, segs, width=0.2, layer="F.Cu", vias=None):
    return Net(name=name,
               features=[NetFeature(layer=layer, width_mm=width, segments=segs)],
               vias=vias or [])


def _dd(nets):
    dd = DesignData(source="test")
    dd.nets = {n.name: n for n in nets}
    return dd


# -- trace_right_angle_bends ------------------------------------------------
def test_right_angle_on_highspeed_flagged():
    # USB_DP with a 90-deg corner at (5,0).
    dd = _dd([_net("/USB_DP", [((0, 0), (5, 0)), ((5, 0), (5, 5))])])
    r = run("trace_right_angle_bends", dd)
    assert r.status == "warning" and "/USB_DP" in r.violations[0].message


def test_straight_highspeed_passes():
    dd = _dd([_net("/USB_DP", [((0, 0), (5, 0)), ((5, 0), (10, 0))])])
    assert run("trace_right_angle_bends", dd).status == "pass"


def test_right_angle_na_without_hs_nets():
    # A 90-deg corner on an ordinary signal is not flagged (folklore guard).
    dd = _dd([_net("GPIO7", [((0, 0), (5, 0)), ((5, 0), (5, 5))])])
    assert run("trace_right_angle_bends", dd).status == "not_applicable"


# -- trace_necking ----------------------------------------------------------
def test_power_necking_flagged():
    # +3V3 runs 0.5 mm, then necks to 0.2 mm over 3 mm.
    dd = _dd([Net(name="+3V3", features=[
        NetFeature(layer="F.Cu", width_mm=0.5, segments=[((0, 0), (10, 0))]),
        NetFeature(layer="F.Cu", width_mm=0.2, segments=[((10, 0), (13, 0))]),
    ])])
    r = run("trace_necking", dd)
    assert r.status == "warning" and "+3V3" in r.violations[0].message


def test_power_uniform_width_passes():
    dd = _dd([Net(name="+3V3", features=[
        NetFeature(layer="F.Cu", width_mm=0.5, segments=[((0, 0), (10, 0)), ((10, 0), (13, 0))])])])
    assert run("trace_necking", dd).status == "pass"


def test_short_neck_is_pad_entry_passes():
    # Narrow run only 0.5 mm (< min_run) -> treated as pad entry, not a bottleneck.
    dd = _dd([Net(name="+3V3", features=[
        NetFeature(layer="F.Cu", width_mm=0.5, segments=[((0, 0), (10, 0))]),
        NetFeature(layer="F.Cu", width_mm=0.2, segments=[((10, 0), (10.5, 0))]),
    ])])
    assert run("trace_necking", dd).status == "pass"


# -- meander_spacing --------------------------------------------------------
def test_tight_meander_flagged():
    # Two parallel USB_DP legs 0.5 mm apart (edge gap 0.3 < 3x0.2).
    dd = _dd([_net("/USB_DP", [((0, 0), (0, 5)), ((0.5, 5), (0.5, 0))], width=0.2)])
    r = run("meander_spacing", dd)
    assert r.status == "warning" and "/USB_DP" in r.violations[0].message


def test_loose_meander_passes():
    dd = _dd([_net("/USB_DP", [((0, 0), (0, 5)), ((2.0, 5), (2.0, 0))], width=0.2)])
    assert run("meander_spacing", dd).status == "pass"


def test_meander_na_without_hs_nets():
    dd = _dd([_net("GPIO7", [((0, 0), (0, 5)), ((0.5, 5), (0.5, 0))], width=0.2)])
    assert run("meander_spacing", dd).status == "not_applicable"


# -- net_via_count ----------------------------------------------------------
def test_excessive_vias_flagged():
    vias = [Via(x_mm=float(i), y_mm=0.0) for i in range(9)]
    dd = _dd([_net("SIG1", [((0, 0), (10, 0))], vias=vias)])
    r = run("net_via_count", dd)
    assert r.status == "warning" and "SIG1" in r.violations[0].message


def test_power_net_many_vias_passes():
    # A ground net with many stitching vias is not flagged.
    vias = [Via(x_mm=float(i), y_mm=0.0) for i in range(12)]
    dd = _dd([_net("GND", [((0, 0), (10, 0))], vias=vias)])
    assert run("net_via_count", dd).status == "pass"


def test_via_count_na_without_vias():
    dd = _dd([_net("SIG1", [((0, 0), (10, 0))])])
    assert run("net_via_count", dd).status == "not_applicable"
