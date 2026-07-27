"""Placement / consistency design-review checks (idea batch B/C)."""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import Component, DesignData, Net, NetPoint, Pad


def run(cid, dd, geometry=None):
    _ensure_impls_loaded()
    ctx = CheckContext(
        check_def=load_check_definition(cid), ingest=None,
        geometry=geometry or BoardGeometry(root_dir=Path(".")),
        geometry_cache=GeometryCache(), ruleset_id="default", design_id="t",
        gerber_zip=Path("x"), design_data=dd)
    return get_check_runner(cid)(ctx)


def _comp(ref, x, y, side="top", rot=0.0, value=None, fp=None, height=None, pads=None):
    return Component(ref=ref, value=value, footprint=fp, x_mm=x, y_mm=y, side=side,
                     rotation_deg=rot, height_mm=height, pads=pads or [])


# -- decoupling_same_side ---------------------------------------------------
def _cap_ic(cap_side):
    # IC U1 pin on VCC at (10,10); cap C1 near it (via its VCC pad point).
    vcc = Net(name="VCC", points=[NetPoint(10, 10, ref="U1", pin="1"),
                                  NetPoint(0, 0, ref="C1", pin="1")])
    gnd = Net(name="GND", points=[NetPoint(11, 10, ref="U1", pin="2"),
                                  NetPoint(0.1, 0, ref="C1", pin="2")])
    dd = DesignData(source="test")
    dd.nets = {"VCC": vcc, "GND": gnd}
    dd.components = [
        _comp("U1", 10, 10, side="top", value="MCU",
              pads=[Pad("1", 10, 10), Pad("2", 11, 10)]),
        _comp("C1", 0, 0, side=cap_side, value="0.1uF",
              pads=[Pad("1", 0, 0), Pad("2", 0.1, 0)]),
    ]
    return dd


def test_decoupling_opposite_side_far_flagged():
    # C1 at (0,0) bottom, IC pin at (10,10) top -> opposite side, ~14 mm away.
    r = run("decoupling_same_side", _cap_ic("bottom"))
    assert r.status == "warning" and "C1" in r.violations[0].message


def test_decoupling_same_side_passes():
    r = run("decoupling_same_side", _cap_ic("top"))
    assert r.status == "pass"


# -- crystal_proximity ------------------------------------------------------
def _crystal(dist):
    xin = Net(name="XIN", points=[NetPoint(0, 0, ref="Y1", pin="1"),
                                  NetPoint(dist, 0, ref="U1", pin="10")])
    dd = DesignData(source="test")
    dd.nets = {"XIN": xin}
    dd.components = [
        _comp("Y1", 0, 0, value="16MHz", pads=[Pad("1", 0, 0), Pad("2", 0.5, 0)]),
        _comp("U1", dist, 0, value="MCU", pads=[Pad("10", dist, 0), Pad("1", dist + 1, 0)]),
    ]
    return dd


def test_crystal_far_flagged():
    r = run("crystal_proximity", _crystal(25.0))
    assert r.status == "warning" and "Y1" in r.violations[0].message


def test_crystal_close_passes():
    assert run("crystal_proximity", _crystal(4.0)).status == "pass"


# -- tall_part_edge_clearance ----------------------------------------------
def _board_geo():
    from pcb_dfm.geometry.layer_model import BoardLayer
    from pcb_dfm.geometry.primitives import Point2D, Polygon
    geo = BoardGeometry(root_dir=Path("."))
    ly = BoardLayer(name="Edge", logical_layer="Outline", side="top", layer_type="outline")
    ly.polygons = [Polygon(vertices=[Point2D(0, 0), Point2D(20, 0), Point2D(20, 20), Point2D(0, 20)])]
    geo.add_layer(ly)
    return geo


def test_tall_part_near_edge_flagged():
    dd = DesignData(source="test")
    dd.components = [_comp("J1", 1.0, 10.0, value="USB", height=8.0,
                          pads=[Pad("1", 1, 10)])]  # 1 mm from x=0 edge, 8 mm tall
    r = run("tall_part_edge_clearance", dd, geometry=_board_geo())
    assert r.status == "warning" and "J1" in r.violations[0].message


def test_tall_part_central_passes():
    dd = DesignData(source="test")
    dd.components = [_comp("J1", 10.0, 10.0, value="USB", height=8.0, pads=[Pad("1", 10, 10)])]
    assert run("tall_part_edge_clearance", dd, geometry=_board_geo()).status == "pass"


def test_tall_part_na_without_heights():
    dd = DesignData(source="test")
    dd.components = [_comp("J1", 1.0, 10.0, value="USB", pads=[Pad("1", 1, 10)])]
    assert run("tall_part_edge_clearance", dd, geometry=_board_geo()).status == "not_applicable"


# -- duplicate_refdes -------------------------------------------------------
def test_duplicate_refdes_flagged():
    dd = DesignData(source="test")
    dd.components = [_comp("R1", 1, 1, pads=[Pad("1", 1, 1)]),
                    _comp("R1", 5, 5, pads=[Pad("1", 5, 5)])]
    r = run("duplicate_refdes", dd)
    assert r.status == "warning" and "R1" in r.violations[0].message


def test_duplicate_refdes_placeholder_ignored():
    # KiCad "REF**" placeholder repeats legitimately -> not flagged.
    dd = DesignData(source="test")
    dd.components = [_comp("REF**", 1, 1, pads=[Pad("1", 1, 1)]),
                    _comp("REF**", 5, 5, pads=[Pad("1", 5, 5)]),
                    _comp("R1", 2, 2, pads=[Pad("1", 2, 2)])]
    assert run("duplicate_refdes", dd).status == "pass"


# -- rail_name_aliasing -----------------------------------------------------
def test_rail_alias_flagged():
    dd = DesignData(source="test")
    dd.nets = {"+3V3": Net(name="+3V3"), "3V3": Net(name="3V3"), "GND": Net(name="GND")}
    dd.components = [_comp("C1", 0, 0)]
    r = run("rail_name_aliasing", dd)
    assert r.status == "warning" and "3V3" in r.violations[0].message


def test_rail_distinct_names_pass():
    dd = DesignData(source="test")
    dd.nets = {"VCC": Net(name="VCC"), "VCC_IO": Net(name="VCC_IO")}
    dd.components = [_comp("C1", 0, 0)]
    assert run("rail_name_aliasing", dd).status == "pass"


# -- polarized_orientation_consistency -------------------------------------
def _leds(angles):
    dd = DesignData(source="test")
    dd.components = [
        _comp(f"LED{i}", float(i), 0.0, rot=a, value="RED", fp="LED_0603",
              pads=[Pad("1", float(i), 0), Pad("2", float(i) + 0.5, 0)])
        for i, a in enumerate(angles)]
    return dd


def test_polarized_outlier_flagged():
    # Five LEDs at 0deg, one flipped to 180deg -> the flipped one is the outlier.
    r = run("polarized_orientation_consistency", _leds([0, 0, 0, 0, 0, 180]))
    assert r.status == "warning" and "LED5" in r.violations[0].message


def test_polarized_consistent_passes():
    assert run("polarized_orientation_consistency", _leds([0, 0, 0, 0, 0])).status == "pass"


def test_polarized_na_small_group():
    assert run("polarized_orientation_consistency", _leds([0, 180])).status == "not_applicable"


def test_all_placement_na_without_design_data():
    for cid in ("decoupling_same_side", "crystal_proximity", "tall_part_edge_clearance",
                "duplicate_refdes", "rail_name_aliasing",
                "polarized_orientation_consistency"):
        assert run(cid, DesignData(source="test")).status == "not_applicable"
