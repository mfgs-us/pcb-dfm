"""Correctness tests for the second batch of Tier-2 design-data checks:
unconnected_pads (#37), power_feed_robustness (#38), decoupling_proximity (#40).

All three are advisory and gated: not_applicable without the design data they
need, warning when they find something, pass when clean.
"""

from __future__ import annotations

import zipfile

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check
from pcb_dfm.ingest.design_model import (
    Component,
    DesignData,
    Net,
    NetPoint,
    Pad,
    Via,
)

_HDR = "%FSLAX46Y46*%\n%MOMM*%\n"


def _flash(cx, cy, w, h, ap="11"):
    return (_HDR + f"%ADD{ap}R,{w:.6f}X{h:.6f}*%\nD{ap}*\n"
            f"X{int(round(cx * 1e6))}Y{int(round(cy * 1e6))}D03*\nM02*\n")


def _zip(tmp_path):
    # Minimal but valid artwork so the geometry loads; the checks read design_data.
    p = tmp_path / "b.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("board.gtl", _flash(5, 5, 1, 1))
        z.writestr("board.gts", _flash(5, 5, 1.2, 1.2))
    return p


def _run(z, cid, dd):
    return run_single_check(z, load_check_definition(cid), design_data=dd)


def _comp(ref, pads, value=None):
    return Component(ref=ref, value=value,
                     pads=[Pad(name=n, x_mm=x, y_mm=y) for (n, x, y) in pads])


# ---- unconnected_pads (#37): a two-terminal part with a floating leg ------
def test_unconnected_na_without_netlist(tmp_path):
    dd = DesignData(components=[_comp("R1", [("1", 1, 1), ("2", 2, 2)])])
    assert _run(_zip(tmp_path), "unconnected_pads", dd).status == "not_applicable"


def test_unconnected_both_legs_connected_passes(tmp_path):
    dd = DesignData(components=[_comp("R1", [("1", 1, 1), ("2", 2, 2)])])
    dd.nets["N1"] = Net(name="N1", points=[NetPoint(x_mm=1, y_mm=1)])
    dd.nets["N2"] = Net(name="N2", points=[NetPoint(x_mm=2, y_mm=2)])
    assert _run(_zip(tmp_path), "unconnected_pads", dd).status == "pass"


def test_unconnected_floating_leg_warns(tmp_path):
    # R1.2 sits on no net access point -> the resistor has a floating leg.
    dd = DesignData(components=[_comp("R1", [("1", 1, 1), ("2", 2, 2)])])
    dd.nets["N1"] = Net(name="N1", points=[NetPoint(x_mm=1, y_mm=1)])
    r = _run(_zip(tmp_path), "unconnected_pads", dd)
    assert r.status == "warning"
    assert r.metric.measured_value == 1


def test_unconnected_ic_unused_pin_not_flagged(tmp_path):
    # An IC with a net-less pin is an intentional no-connect -> must NOT flag.
    dd = DesignData(components=[_comp("U1", [("1", 1, 1), ("2", 2, 2), ("3", 3, 3)])])
    dd.nets["N1"] = Net(name="N1", points=[NetPoint(x_mm=1, y_mm=1)])
    dd.nets["N2"] = Net(name="N2", points=[NetPoint(x_mm=2, y_mm=2)])
    # U1.3 is net-less but an IC -> not our concern.
    assert _run(_zip(tmp_path), "unconnected_pads", dd).status == "pass"


def test_unconnected_fully_floating_part_not_flagged(tmp_path):
    # A resistor with BOTH legs net-less is mechanical / unplaced -> not flagged.
    dd = DesignData(components=[_comp("R9", [("1", 8, 8), ("2", 9, 9)])])
    dd.nets["N1"] = Net(name="N1", points=[NetPoint(x_mm=1, y_mm=1)])
    assert _run(_zip(tmp_path), "unconnected_pads", dd).status == "pass"


# ---- power_feed_robustness (#38) -----------------------------------------
def test_power_feed_na_without_vias(tmp_path):
    dd = DesignData()
    dd.nets["VCC"] = Net(name="VCC", points=[NetPoint(x_mm=1, y_mm=1)])
    assert _run(_zip(tmp_path), "power_feed_robustness", dd).status == "not_applicable"


def test_power_feed_single_via_warns(tmp_path):
    dd = DesignData()
    dd.nets["VCC"] = Net(name="VCC", vias=[Via(x_mm=1, y_mm=1,
                                               from_layer="F.Cu", to_layer="B.Cu")])
    dd.nets["GND"] = Net(name="GND", vias=[Via(x_mm=2, y_mm=2), Via(x_mm=3, y_mm=3)])
    r = _run(_zip(tmp_path), "power_feed_robustness", dd)
    assert r.status == "warning"
    assert r.metric.measured_value == 1  # only VCC is a SPOF


def test_power_feed_redundant_vias_pass(tmp_path):
    dd = DesignData()
    dd.nets["VCC"] = Net(name="VCC", vias=[Via(x_mm=1, y_mm=1), Via(x_mm=2, y_mm=2)])
    dd.nets["SIG"] = Net(name="SIG", vias=[Via(x_mm=5, y_mm=5)])  # signal, ignored
    assert _run(_zip(tmp_path), "power_feed_robustness", dd).status == "pass"


# ---- decoupling_proximity (#40) ------------------------------------------
def _decap_ic(cap_x, ic_x):
    """A 100nF cap and an IC, both on net VCC; caller sets the X separation."""
    dd = DesignData(components=[
        _comp("C1", [("1", cap_x, 0)], value="100nF"),
        _comp("U1", [("1", ic_x, 0)]),
    ])
    dd.nets["VCC"] = Net(name="VCC", points=[
        NetPoint(x_mm=cap_x, y_mm=0), NetPoint(x_mm=ic_x, y_mm=0)])
    return dd


def test_decoupling_na_without_pair(tmp_path):
    dd = DesignData(components=[_comp("C1", [("1", 0, 0)], value="100nF")])
    dd.nets["VCC"] = Net(name="VCC", points=[NetPoint(x_mm=0, y_mm=0)])
    assert _run(_zip(tmp_path), "decoupling_proximity", dd).status == "not_applicable"


def test_decoupling_far_cap_warns(tmp_path):
    dd = _decap_ic(cap_x=0.0, ic_x=10.0)  # 10 mm apart -> beyond 5 mm limit
    r = _run(_zip(tmp_path), "decoupling_proximity", dd)
    assert r.status == "warning"
    assert abs(r.metric.measured_value - 10.0) < 1e-6


def test_decoupling_close_cap_passes(tmp_path):
    dd = _decap_ic(cap_x=0.0, ic_x=2.0)  # 2 mm apart -> fine
    assert _run(_zip(tmp_path), "decoupling_proximity", dd).status == "pass"
