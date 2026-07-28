"""Pad geometry (size/shape/rotation) + the checks it unblocks."""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import Component, DesignData, Net, NetFeature, NetPoint, Pad, Via


# -- Pad.contains / area ----------------------------------------------------
def test_pad_contains_rect():
    p = Pad(name="1", x_mm=10.0, y_mm=5.0, width_mm=1.0, height_mm=0.5, shape="rect")
    assert p.contains(10.0, 5.0)            # centre
    assert p.contains(10.45, 5.2)           # inside the 1.0 x 0.5 box
    assert not p.contains(10.0, 5.4)        # 0.4 above centre, past the 0.25 half-height
    assert p.contains(10.0, 5.35, margin_mm=0.15)  # margin brings it in


def test_pad_contains_rotated():
    # A 1.0 x 0.2 pad rotated 90 deg is tall/narrow.
    p = Pad(name="1", x_mm=0.0, y_mm=0.0, width_mm=1.0, height_mm=0.2, shape="rect",
            rotation_deg=90.0)
    assert p.contains(0.0, 0.45)            # along the (now vertical) length
    assert not p.contains(0.45, 0.0)        # across the (now narrow) width


def test_pad_area():
    assert Pad(name="1", x_mm=0, y_mm=0, width_mm=2.0, height_mm=3.0).area_mm2() == 6.0
    assert Pad(name="1", x_mm=0, y_mm=0).area_mm2() is None


def _run(cid, dd):
    _ensure_impls_loaded()
    ctx = CheckContext(
        check_def=load_check_definition(cid), ingest=None,
        geometry=BoardGeometry(root_dir=Path(".")), geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=dd)
    return get_check_runner(cid)(ctx)


# -- thermal_pad_via_count --------------------------------------------------
def _ep_ic(via_under: bool):
    pads = [Pad(name="1", x_mm=0, y_mm=0, width_mm=0.5, height_mm=0.5, shape="rect"),
            Pad(name="2", x_mm=1, y_mm=0, width_mm=0.5, height_mm=0.5, shape="rect"),
            Pad(name="EP", x_mm=0.5, y_mm=-2, width_mm=3.0, height_mm=3.0, shape="rect")]
    dd = DesignData(source="test")
    dd.components = [Component(ref="U1", value="QFN", pads=pads)]
    if via_under:
        dd.nets = {"GND": Net(name="GND", vias=[Via(x_mm=0.5, y_mm=-2)])}
    return dd


def test_thermal_pad_no_via_flagged():
    r = _run("thermal_pad_via_count", _ep_ic(via_under=False))
    assert r.status == "warning" and "U1" in r.violations[0].message


def test_thermal_pad_with_via_passes():
    r = _run("thermal_pad_via_count", _ep_ic(via_under=True))
    assert r.status == "pass"


def test_thermal_na_no_exposed_pad():
    dd = DesignData(source="test")
    dd.components = [Component(ref="U1", value="SOT", pads=[
        Pad(name="1", x_mm=0, y_mm=0, width_mm=0.5, height_mm=0.5),
        Pad(name="2", x_mm=1, y_mm=0, width_mm=0.5, height_mm=0.5)])]
    assert _run("thermal_pad_via_count", dd).status == "not_applicable"


# -- unrouted_or_partial_net (zero-routing) --------------------------------
def _two_pin(routed: bool):
    dd = DesignData(source="test")
    sig = Net(name="SIG", points=[NetPoint(0, 0, ref="U1", pin="1"),
                                  NetPoint(5, 0, ref="U2", pin="1")])
    if routed:
        sig.features = [NetFeature(layer="F.Cu", width_mm=0.2, segments=[((0, 0), (5, 0))])]
    dd.nets = {"SIG": sig}
    dd.components = [
        Component(ref="U1", value="A", pads=[Pad("1", 0, 0, width_mm=0.5, height_mm=0.5)]),
        Component(ref="U2", value="B", pads=[Pad("1", 5, 0, width_mm=0.5, height_mm=0.5)]),
    ]
    return dd


def test_unrouted_net_flagged():
    r = _run("unrouted_or_partial_net", _two_pin(routed=False))
    assert r.status == "warning" and "SIG" in r.violations[0].message


def test_routed_net_passes():
    assert _run("unrouted_or_partial_net", _two_pin(routed=True)).status == "pass"


def test_unrouted_na_without_pad_geometry():
    dd = DesignData(source="test")
    sig = Net(name="SIG", points=[NetPoint(0, 0, ref="U1", pin="1"),
                                  NetPoint(5, 0, ref="U2", pin="1")])
    dd.nets = {"SIG": sig}
    dd.components = [Component(ref="U1", value="A", pads=[Pad("1", 0, 0)]),
                    Component(ref="U2", value="B", pads=[Pad("1", 5, 0)])]
    assert _run("unrouted_or_partial_net", dd).status == "not_applicable"
