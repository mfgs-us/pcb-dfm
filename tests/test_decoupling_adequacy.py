"""decoupling_adequacy -- first electrical-correctness design-review check.

Builds a tiny netlist + BOM (nets with access points, components with coincident
pads) so PadNetIndex resolves rails, then asserts the review outcome.
"""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import Component, DesignData, Net, NetPoint, Pad


def _pad(name, x, y):
    return Pad(name=name, x_mm=x, y_mm=y)


def _pt(x, y, ref, pin):
    return NetPoint(x_mm=x, y_mm=y, ref=ref, pin=pin)


def _run(dd: DesignData):
    _ensure_impls_loaded()
    ctx = CheckContext(
        check_def=load_check_definition("decoupling_adequacy"),
        ingest=None, geometry=BoardGeometry(root_dir=Path(".")),
        geometry_cache=GeometryCache(), ruleset_id="default", design_id="t",
        gerber_zip=Path("x"), design_data=dd)
    return get_check_runner("decoupling_adequacy")(ctx)


def _dd(nets, comps):
    dd = DesignData(source="test")
    dd.nets = {n.name: n for n in nets}
    dd.components = comps
    return dd


def test_pass_rail_with_bypass_cap():
    # U1 (IC) on VCC+GND; C1 (cap) bridges VCC+GND -> rail decoupled.
    vcc = Net(name="VCC", net_class="power",
              points=[_pt(0, 0, "U1", "1"), _pt(0.5, 0, "C1", "1")])
    gnd = Net(name="GND", net_class="ground",
              points=[_pt(2, 0, "U1", "2"), _pt(0.5, 1, "C1", "2")])
    u1 = Component(ref="U1", value="MCU", pads=[_pad("1", 0, 0), _pad("2", 2, 0)])
    c1 = Component(ref="C1", value="0.1uF", pads=[_pad("1", 0.5, 0), _pad("2", 0.5, 1)])
    r = _run(_dd([vcc, gnd], [u1, c1]))
    assert r.status == "pass"
    assert r.metric.measured_value == 0.0


def test_warns_rail_without_any_bypass_cap():
    # U1 (IC) on 3V3+GND, but nothing bypasses 3V3 -> flagged.
    r3 = Net(name="+3V3", net_class="power", points=[_pt(0, 0, "U1", "1")])
    gnd = Net(name="GND", net_class="ground", points=[_pt(2, 0, "U1", "2")])
    u1 = Component(ref="U1", value="MCU", pads=[_pad("1", 0, 0), _pad("2", 2, 0)])
    r = _run(_dd([r3, gnd], [u1]))
    assert r.status == "warning"
    assert r.metric.measured_value == 1.0
    assert "+3V3" in r.violations[0].message


def test_connector_only_rail_is_not_flagged():
    # VBUS feeds only a connector J1 (no IC) and has no cap -> NOT a decoupling
    # concern. VCC feeds U1 and is bypassed -> the board passes cleanly.
    vbus = Net(name="VBUS", net_class="power", points=[_pt(5, 5, "J1", "1")])
    vcc = Net(name="VCC", net_class="power",
              points=[_pt(0, 0, "U1", "1"), _pt(0.5, 0, "C1", "1")])
    gnd = Net(name="GND", net_class="ground",
              points=[_pt(2, 0, "U1", "2"), _pt(0.5, 1, "C1", "2"), _pt(6, 5, "J1", "2")])
    j1 = Component(ref="J1", value="USB", pads=[_pad("1", 5, 5), _pad("2", 6, 5)])
    u1 = Component(ref="U1", value="MCU", pads=[_pad("1", 0, 0), _pad("2", 2, 0)])
    c1 = Component(ref="C1", value="100nF", pads=[_pad("1", 0.5, 0), _pad("2", 0.5, 1)])
    r = _run(_dd([vbus, vcc, gnd], [j1, u1, c1]))
    assert r.status == "pass"


def test_not_applicable_without_ground():
    vcc = Net(name="VCC", net_class="power", points=[_pt(0, 0, "U1", "1")])
    sig = Net(name="SIG", net_class="signal", points=[_pt(2, 0, "U1", "2")])
    u1 = Component(ref="U1", value="MCU", pads=[_pad("1", 0, 0), _pad("2", 2, 0)])
    r = _run(_dd([vcc, sig], [u1]))
    assert r.status == "not_applicable"


def test_not_applicable_without_ics():
    # Only passives on the rail -> nothing that needs decoupling.
    vcc = Net(name="VCC", net_class="power", points=[_pt(0, 0, "R1", "1")])
    gnd = Net(name="GND", net_class="ground", points=[_pt(2, 0, "R1", "2")])
    r1 = Component(ref="R1", value="10k", pads=[_pad("1", 0, 0), _pad("2", 2, 0)])
    r = _run(_dd([vcc, gnd], [r1]))
    assert r.status == "not_applicable"


def test_not_applicable_without_design_data():
    r = _run(DesignData(source="test"))
    assert r.status == "not_applicable"
