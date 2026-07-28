"""Pin-type (schematic) checks: contention, no-driver, open-drain pull-up."""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import Component, DesignData, Net, NetPoint, Pad


def build(specs, pin_types=None):
    # specs: [(ref, [(pad, x, y, net), ...]), ...]
    dd = DesignData(source="test")
    nets, comps = {}, []
    for ref, pads in specs:
        padobjs = []
        for (pn, x, y, net) in pads:
            padobjs.append(Pad(name=pn, x_mm=x, y_mm=y))
            nets.setdefault(net, Net(name=net)).points.append(
                NetPoint(x_mm=x, y_mm=y, ref=ref, pin=pn))
        comps.append(Component(ref=ref, pads=padobjs))
    dd.nets, dd.components = nets, comps
    dd.pin_types = pin_types or {}
    return dd


def run(cid, dd):
    _ensure_impls_loaded()
    ctx = CheckContext(
        check_def=load_check_definition(cid), ingest=None,
        geometry=BoardGeometry(root_dir=Path(".")), geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=dd)
    return get_check_runner(cid)(ctx)


# -- output_drive_contention ------------------------------------------------
def test_two_outputs_flagged():
    dd = build([("U1", [("1", 0, 0, "SIG")]), ("U2", [("1", 5, 0, "SIG")])],
               pin_types={("U1", "1"): "output", ("U2", "1"): "output"})
    r = run("output_drive_contention", dd)
    assert r.status == "warning" and "SIG" in r.violations[0].message


def test_output_plus_input_passes():
    dd = build([("U1", [("1", 0, 0, "SIG")]), ("U2", [("1", 5, 0, "SIG")])],
               pin_types={("U1", "1"): "output", ("U2", "1"): "input"})
    assert run("output_drive_contention", dd).status == "pass"


def test_shared_bus_not_contention():
    # Two outputs but the net also has a tri-state pin -> a bus, not contention.
    dd = build([("U1", [("1", 0, 0, "BUS")]), ("U2", [("1", 5, 0, "BUS")]),
                ("U3", [("1", 8, 0, "BUS")])],
               pin_types={("U1", "1"): "output", ("U2", "1"): "output",
                          ("U3", "1"): "tri_state"})
    assert run("output_drive_contention", dd).status == "pass"


def test_contention_na_without_pin_types():
    dd = build([("U1", [("1", 0, 0, "SIG")]), ("U2", [("1", 5, 0, "SIG")])])
    assert run("output_drive_contention", dd).status == "not_applicable"


# -- net_without_driver -----------------------------------------------------
def test_only_inputs_flagged():
    dd = build([("U1", [("1", 0, 0, "SIG")]), ("U2", [("1", 5, 0, "SIG")])],
               pin_types={("U1", "1"): "input", ("U2", "1"): "input"})
    r = run("net_without_driver", dd)
    assert r.status == "warning" and "SIG" in r.violations[0].message


def test_input_with_driver_passes():
    dd = build([("U1", [("1", 0, 0, "SIG")]), ("U2", [("1", 5, 0, "SIG")])],
               pin_types={("U1", "1"): "output", ("U2", "1"): "input"})
    assert run("net_without_driver", dd).status == "pass"


def test_input_with_passive_bias_passes():
    # Input + a pull resistor (passive) -> biased, not floating.
    dd = build([("U1", [("1", 0, 0, "SIG")]), ("R1", [("1", 5, 0, "SIG")])],
               pin_types={("U1", "1"): "input", ("R1", "1"): "passive"})
    assert run("net_without_driver", dd).status == "pass"


# -- open_drain_pullup ------------------------------------------------------
def test_open_drain_no_pullup_flagged():
    dd = build([("U1", [("1", 0, 0, "IRQ"), ("2", 0, 1, "VCC")]),
                ("U2", [("1", 5, 0, "VCC"), ("2", 5, 1, "GND")])],
               pin_types={("U1", "1"): "open_collector"})
    r = run("open_drain_pullup", dd)
    assert r.status == "warning" and "IRQ" in r.violations[0].message


def test_open_drain_with_pullup_passes():
    dd = build([("U1", [("1", 0, 0, "IRQ")]),
                ("R1", [("1", 0.01, 0, "IRQ"), ("2", 1, 0, "VCC")]),
                ("U2", [("1", 5, 0, "VCC")])],
               pin_types={("U1", "1"): "open_collector"})
    assert run("open_drain_pullup", dd).status == "pass"


def test_open_drain_na_without_pin_types():
    dd = build([("U1", [("1", 0, 0, "IRQ"), ("2", 0, 1, "VCC")])])
    assert run("open_drain_pullup", dd).status == "not_applicable"


# -- critical_pin_connectivity ----------------------------------------------
def test_unpowered_part_flagged():
    # U1's only power_in pin lands on a signal net -> no rail -> unpowered part.
    dd = build([("U1", [("1", 0, 0, "SIG")]), ("U2", [("1", 5, 0, "SIG")])],
               pin_types={("U1", "1"): "power_in"})
    r = run("critical_pin_connectivity", dd)
    assert r.status == "warning" and "U1" in r.violations[0].message


def test_dead_end_power_pin_flagged():
    # Power pin on a power-named net that has nothing else on it -> dead end.
    dd = build([("U1", [("1", 0, 0, "VBAT_X")])],
               pin_types={("U1", "1"): "power_in"})
    r = run("critical_pin_connectivity", dd)
    assert r.status == "warning"


def test_powered_part_passes():
    dd = build([("U1", [("1", 0, 0, "GND")]), ("U2", [("1", 5, 0, "GND")])],
               pin_types={("U1", "1"): "power_in"})
    assert run("critical_pin_connectivity", dd).status == "pass"


def test_critical_pin_na_without_pin_types():
    dd = build([("U1", [("1", 0, 0, "GND")]), ("U2", [("1", 5, 0, "GND")])])
    assert run("critical_pin_connectivity", dd).status == "not_applicable"
