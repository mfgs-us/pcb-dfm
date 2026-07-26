"""Unit tests for the Tier-2 design-intelligence enablers (E2, E3, E4) and the
E5 keep-out region ingestion.

These are pure-data helpers, so the tests are small and exact -- they pin the
classification and pad-net resolution the design checks will build on.
"""

from __future__ import annotations

from pcb_dfm.ingest.design_data import load_design_data
from pcb_dfm.ingest.design_intel import (
    build_pad_net_index,
    classify_component,
    classify_net,
    is_decoupling_candidate,
    is_power_or_ground,
)
from pcb_dfm.ingest.design_model import Component, DesignData, Net, NetPoint, Pad


# ---- E3: net function classifier -----------------------------------------
def test_ground_nets():
    for n in ["GND", "gnd", "VSS", "AGND", "DGND", "GND1", "EARTH"]:
        assert classify_net(n) == "ground", n


def test_power_nets():
    for n in ["VCC", "VDD", "+3V3", "5V", "VBAT", "3V3", "VDDIO", "-12V", "1V8"]:
        assert classify_net(n) == "power", n


def test_signal_nets():
    for n in ["N$3", "CLK_P", "USB_DP", "SDA", "NET42", ""]:
        assert classify_net(n) == "signal", n


def test_net_class_hint_wins():
    assert classify_net("MYRAIL", net_class="Power") == "power"
    assert is_power_or_ground("GND") and is_power_or_ground("VCC")
    assert not is_power_or_ground("DATA0")


# ---- E4: component classifier ---------------------------------------------
def test_class_from_refdes():
    cases = {"R1": "resistor", "C10": "capacitor", "L2": "inductor",
             "D3": "diode", "U1": "ic", "Q2": "transistor", "J5": "connector",
             "LED1": "led", "Y1": "crystal", "FB1": "ferrite"}
    for ref, cls in cases.items():
        got, _ = classify_component(Component(ref=ref))
        assert got == cls, f"{ref} -> {got}, want {cls}"


def test_polarity_inference():
    assert classify_component(Component(ref="D1"))[1] is True     # diode
    assert classify_component(Component(ref="LED2"))[1] is True   # led
    assert classify_component(Component(ref="R5"))[1] is False    # resistor
    # ceramic cap not polarized; electrolytic is
    assert classify_component(Component(ref="C1", footprint="C_0402"))[1] is False
    assert classify_component(Component(ref="C2", footprint="CP_Elec_6.3x5.4"))[1] is True


def test_bom_part_class_wins():
    got, _ = classify_component(Component(ref="X1", part_class="resistor"))
    assert got == "resistor"


def test_decoupling_candidate():
    assert is_decoupling_candidate(Component(ref="C1", value="100nF"))
    assert is_decoupling_candidate(Component(ref="C2", value="0.1uF"))
    assert not is_decoupling_candidate(Component(ref="C3", value="47uF"))  # bulk
    assert not is_decoupling_candidate(Component(ref="R1", value="10k"))   # not a cap


# ---- E2: pad <-> net <-> component resolver -------------------------------
def test_pad_net_index_matches_by_location():
    dd = DesignData()
    dd.nets["GND"] = Net(name="GND", points=[NetPoint(x_mm=1.0, y_mm=1.0),
                                             NetPoint(x_mm=5.0, y_mm=5.0)])
    dd.nets["SIG"] = Net(name="SIG", points=[NetPoint(x_mm=1.0, y_mm=2.0)])
    dd.components = [
        Component(ref="R1", pads=[Pad(name="1", x_mm=1.0, y_mm=1.0),
                                  Pad(name="2", x_mm=1.0, y_mm=2.0)]),
        Component(ref="C1", pads=[Pad(name="1", x_mm=5.0, y_mm=5.0),
                                  Pad(name="2", x_mm=9.0, y_mm=9.0)]),  # no net here
    ]
    idx = build_pad_net_index(dd)
    assert idx.pad_net[("R1", "1")] == "GND"
    assert idx.pad_net[("R1", "2")] == "SIG"
    assert idx.pad_net[("C1", "1")] == "GND"
    assert ("C1", "2") in idx.unmatched_pads
    assert idx.nets_of("R1") == {"GND", "SIG"}
    assert "R1" in idx.components_on("GND") and "C1" in idx.components_on("GND")


def test_pad_net_index_on_real_ipc_netlist(tmp_path):
    from pathlib import Path
    ipc = Path("testdata/pcbtools_full.ipc")
    if not ipc.exists():
        import pytest
        pytest.skip("pcbtools_full.ipc not present")
    dd = load_design_data(ipc)
    idx = build_pad_net_index(dd)
    # Every pad on this real board resolves to a net (verified: 65/65).
    assert len(idx.unmatched_pads) == 0
    assert len(idx.pad_net) > 0
    assert len(idx.components_on("GND")) >= 5


# ---- E5: keep-out region ingestion ----------------------------------------
def test_sidecar_keepout_region():
    dd = load_design_data({"keepouts": [
        {"kind": "antenna", "name": "BLE", "layers": ["F.Cu", "B.Cu"],
         "polygon": [[0, 0], [10, 0], [10, 5], [0, 5]]},
        {"kind": "keepout", "polygon": [[1, 1]]},  # too few verts -> dropped
    ]})
    assert len(dd.keepouts) == 1
    k = dd.keepouts[0]
    assert k.kind == "antenna" and k.name == "BLE"
    assert len(k.polygon) == 4 and k.layers == ["F.Cu", "B.Cu"]
