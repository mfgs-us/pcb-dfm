"""Electrical design-review checks (idea batch A) -- correctness + FP guards.

Each fixture is a tiny netlist + BOM: nets carry access points, components carry
coincident pads, so PadNetIndex resolves. Net names drive classify_net (VCC/GND/
SDA/...); ref prefixes drive classify_component (U=ic, R=resistor, C=capacitor,
Y=crystal, LED=led, J=connector, TP=testpoint).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import (
    Component,
    DesignData,
    DiffPair,
    Net,
    NetPoint,
    Pad,
)

# (pad_name, x, y, net_name)
PadSpec = Tuple[str, float, float, str]
# (ref, value, [PadSpec, ...])
CompSpec = Tuple[str, Optional[str], List[PadSpec]]


def build(specs: List[CompSpec], diff_pairs=None) -> DesignData:
    dd = DesignData(source="test")
    nets = {}
    comps = []
    for ref, value, pads in specs:
        padobjs = []
        for (pn, x, y, net) in pads:
            padobjs.append(Pad(name=pn, x_mm=x, y_mm=y))
            nets.setdefault(net, Net(name=net)).points.append(
                NetPoint(x_mm=x, y_mm=y, ref=ref, pin=pn))
        comps.append(Component(ref=ref, value=value, pads=padobjs))
    dd.nets = nets
    dd.components = comps
    if diff_pairs:
        dd.diff_pairs = diff_pairs
    return dd


def run(cid: str, dd: DesignData):
    _ensure_impls_loaded()
    ctx = CheckContext(
        check_def=load_check_definition(cid), ingest=None,
        geometry=BoardGeometry(root_dir=Path(".")), geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=dd)
    return get_check_runner(cid)(ctx)


# -- floating_or_single_pin_net --------------------------------------------
def test_floating_net_flagged():
    dd = build([
        ("U1", "MCU", [("1", 1, 0, "VCC"), ("2", 2, 0, "GND"), ("3", 3, 0, "DANGLE")]),
        ("R1", "10k", [("1", 4, 0, "VCC"), ("2", 5, 0, "GND")]),
    ])
    r = run("floating_or_single_pin_net", dd)
    assert r.status == "warning" and r.metric.measured_value == 1.0
    assert "DANGLE" in r.violations[0].message


def test_floating_net_clean_passes():
    dd = build([
        ("U1", "MCU", [("1", 1, 0, "VCC"), ("2", 2, 0, "GND")]),
        ("R1", "10k", [("1", 3, 0, "VCC"), ("2", 4, 0, "GND")]),
    ])
    assert run("floating_or_single_pin_net", dd).status == "pass"


# -- unpowered_ic -----------------------------------------------------------
def _big_ic(gnd_net: str):
    pads = [(str(i), float(i), 0.0, ("VCC" if i == 1 else f"SIG{i}")) for i in range(1, 8)]
    pads.append(("8", 8.0, 0.0, gnd_net))
    return ("U1", "BGA", pads)


def test_unpowered_ic_flagged_when_no_ground():
    # 8-pin IC fully resolved, none of its nets is ground; GND exists via C1.
    dd = build([
        _big_ic("SIG8"),
        ("C1", "1uF", [("1", 20, 0, "VCC"), ("2", 21, 0, "GND")]),
    ])
    r = run("unpowered_ic", dd)
    assert r.status == "warning" and "U1" in r.violations[0].message


def test_unpowered_ic_passes_with_ground():
    dd = build([_big_ic("GND")])
    assert run("unpowered_ic", dd).status == "pass"


def test_unpowered_ic_skips_small_part():
    # A SOT-23-6-style 6-pin part referencing a non-GND net is NOT flagged.
    dd = build([
        ("U9", "SOT23", [("1", 1, 0, "VBAT"), ("2", 2, 0, "BAT_NEG"),
                         ("3", 3, 0, "CS"), ("4", 4, 0, "GATE"),
                         ("5", 5, 0, "OD"), ("6", 6, 0, "OC")]),
        ("C1", "1uF", [("1", 20, 0, "VBAT"), ("2", 21, 0, "GND")]),
    ])
    assert run("unpowered_ic", dd).status == "pass"


# -- crystal_load_caps ------------------------------------------------------
def test_crystal_missing_load_caps_flagged():
    dd = build([
        ("Y1", "16MHz", [("1", 1, 0, "XIN"), ("2", 2, 0, "XOUT")]),
        ("C1", "1uF", [("1", 20, 0, "VCC"), ("2", 21, 0, "GND")]),  # unrelated -> GND exists
    ])
    r = run("crystal_load_caps", dd)
    assert r.status == "warning" and "Y1" in r.violations[0].message


def test_crystal_with_load_caps_passes():
    dd = build([
        ("Y1", "16MHz", [("1", 1, 0, "XIN"), ("2", 2, 0, "XOUT")]),
        ("C1", "18pF", [("1", 1.01, 0, "XIN"), ("2", 3, 0, "GND")]),
        ("C2", "18pF", [("1", 2.01, 0, "XOUT"), ("2", 4, 0, "GND")]),
    ])
    assert run("crystal_load_caps", dd).status == "pass"


# -- led_series_resistor ----------------------------------------------------
def test_led_across_rail_flagged():
    dd = build([("LED1", "RED", [("1", 1, 0, "VCC"), ("2", 2, 0, "GND")])])
    r = run("led_series_resistor", dd)
    assert r.status == "warning" and "LED1" in r.violations[0].message


def test_led_with_series_resistor_passes():
    dd = build([
        ("LED1", "RED", [("1", 1, 0, "VCC"), ("2", 2, 0, "LEDK")]),
        ("R1", "330", [("1", 2.01, 0, "LEDK"), ("2", 3, 0, "GND")]),
    ])
    assert run("led_series_resistor", dd).status == "pass"


# -- i2c_pullup_presence ----------------------------------------------------
def test_i2c_without_pullup_flagged():
    dd = build([
        ("U1", "MCU", [("1", 1, 0, "SDA"), ("2", 2, 0, "SCL"), ("3", 3, 0, "VCC")]),
    ])
    r = run("i2c_pullup_presence", dd)
    assert r.status == "warning" and r.metric.measured_value == 2.0


def test_i2c_with_pullups_passes():
    dd = build([
        ("U1", "MCU", [("1", 1, 0, "SDA"), ("2", 2, 0, "SCL"), ("3", 3, 0, "VCC")]),
        ("R1", "4k7", [("1", 1.01, 0, "SDA"), ("2", 4, 0, "VCC")]),
        ("R2", "4k7", [("1", 2.01, 0, "SCL"), ("2", 5, 0, "VCC")]),
    ])
    assert run("i2c_pullup_presence", dd).status == "pass"


# -- reset_pullup_presence --------------------------------------------------
def test_reset_without_bias_flagged():
    dd = build([("U1", "MCU", [("1", 1, 0, "NRST"), ("2", 2, 0, "VCC")])])
    r = run("reset_pullup_presence", dd)
    assert r.status == "warning" and "NRST" in r.violations[0].message


def test_reset_with_pullup_passes():
    dd = build([
        ("U1", "MCU", [("1", 1, 0, "NRST"), ("2", 2, 0, "VCC")]),
        ("R1", "10k", [("1", 1.01, 0, "NRST"), ("2", 3, 0, "VCC")]),
    ])
    assert run("reset_pullup_presence", dd).status == "pass"


# -- bulk_capacitance_present ----------------------------------------------
def test_bulk_absent_flagged():
    dd = build([
        ("U1", "MCU", [("1", 1, 0, "VCC"), ("2", 2, 0, "GND")]),
        ("C1", "0.1uF", [("1", 1.01, 0, "VCC"), ("2", 3, 0, "GND")]),
    ])
    r = run("bulk_capacitance_present", dd)
    assert r.status == "warning"


def test_bulk_present_passes():
    dd = build([
        ("U1", "MCU", [("1", 1, 0, "VCC"), ("2", 2, 0, "GND")]),
        ("C1", "0.1uF", [("1", 1.01, 0, "VCC"), ("2", 3, 0, "GND")]),
        ("C2", "10uF", [("1", 4, 0, "VCC"), ("2", 5, 0, "GND")]),
    ])
    assert run("bulk_capacitance_present", dd).status == "pass"


# -- differential_pair_completeness ----------------------------------------
def test_diff_pair_missing_member_flagged():
    dd = DesignData(source="test")
    dd.nets = {"USB_DP": Net(name="USB_DP")}  # DN absent
    dd.diff_pairs = [DiffPair(name="USB", positive="USB_DP", negative="USB_DN")]
    r = run("differential_pair_completeness", dd)
    assert r.status == "warning" and "USB" in r.violations[0].message


def test_diff_pair_complete_passes():
    dd = DesignData(source="test")
    dd.nets = {"USB_DP": Net(name="USB_DP"), "USB_DN": Net(name="USB_DN")}
    dd.diff_pairs = [DiffPair(name="USB", positive="USB_DP", negative="USB_DN")]
    assert run("differential_pair_completeness", dd).status == "pass"


# -- debug_port_test_access -------------------------------------------------
def test_debug_without_access_flagged():
    dd = build([("U1", "MCU", [("1", 1, 0, "SWDIO"), ("2", 2, 0, "SWCLK"),
                               ("3", 3, 0, "GND")])])
    r = run("debug_port_test_access", dd)
    assert r.status == "warning" and r.metric.measured_value == 2.0


def test_debug_with_testpoint_passes():
    dd = build([
        ("U1", "MCU", [("1", 1, 0, "SWDIO"), ("2", 2, 0, "SWCLK"), ("3", 3, 0, "GND")]),
        ("TP1", None, [("1", 1.01, 0, "SWDIO")]),
        ("TP2", None, [("1", 2.01, 0, "SWCLK")]),
    ])
    assert run("debug_port_test_access", dd).status == "pass"


def test_all_electrical_na_without_design_data():
    for cid in ("floating_or_single_pin_net", "unpowered_ic", "crystal_load_caps",
                "led_series_resistor", "i2c_pullup_presence", "reset_pullup_presence",
                "bulk_capacitance_present", "differential_pair_completeness",
                "debug_port_test_access"):
        assert run(cid, DesignData(source="test")).status == "not_applicable"
