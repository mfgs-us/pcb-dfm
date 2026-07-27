"""
Tests for the design-data model, its adapters (JSON sidecar + IPC-2581), and
the connectivity-aware checks that consume it (impedance, dielectric, diff-pair
skew). The IPC-2581 fixture is committed at testdata/sample_design.xml.
"""

from pathlib import Path

import pytest

from pcb_dfm.ingest.design_data import load_design_data

_REPO = Path(__file__).resolve().parent.parent
IPC = _REPO / "testdata" / "sample_design.xml"
GERBER = _REPO / "testdata" / "mini_board.zip"


# --------------------------------------------------------------------------
# Adapters -> DesignData
# --------------------------------------------------------------------------

def test_sidecar_adapter_builds_stackup_and_specs():
    dd = load_design_data({
        "stackup": {"er": 4.2, "dielectric_thickness_mm": 0.20, "copper_thickness_mm": 0.035},
        "controlled_impedance": [{"name": "CLK", "width_mm": 0.20, "target_ohm": 50.0}],
    })
    assert dd.source == "sidecar"
    assert dd.stackup.er == pytest.approx(4.2)
    assert dd.stackup.dielectric_thickness_mm == pytest.approx(0.20)
    assert dd.stackup.copper_thickness_mm == pytest.approx(0.035)
    assert len(dd.controlled_impedance) == 1
    assert dd.controlled_impedance[0].target_ohm == pytest.approx(50.0)


def test_ipc2581_adapter_parses_stackup_nets_and_diffpairs():
    dd = load_design_data(IPC)
    assert dd.source == "ipc2581"

    # Stackup: 2 copper + 2 dielectric, Er and thicknesses in mm.
    assert len(dd.stackup.copper_layers()) == 2
    assert len(dd.stackup.dielectric_layers()) == 2
    assert dd.stackup.er == pytest.approx(4.3)
    assert dd.stackup.dielectric_thickness_mm == pytest.approx(0.20)
    assert dd.stackup.copper_thickness_mm == pytest.approx(0.035)

    # Nets + routed length summed from Line geometry.
    assert dd.net("CLK_P").routed_length_mm() == pytest.approx(20.0)
    assert dd.net("CLK_N").routed_length_mm() == pytest.approx(21.2)

    # Diff pair inferred from CLK_P / CLK_N naming.
    assert len(dd.diff_pairs) == 1
    dp = dd.diff_pairs[0]
    assert {dp.positive, dp.negative} == {"CLK_P", "CLK_N"}

    # Controlled-impedance hint on the RF net.
    names = {c.name for c in dd.controlled_impedance}
    assert "RF" in names


# --------------------------------------------------------------------------
# Checks consuming DesignData (driven from the IPC-2581 fixture)
# --------------------------------------------------------------------------

pytest.importorskip("gerbonara", reason="gerbonara not installed")
pytestmark = pytest.mark.skipif(not GERBER.exists(), reason="gerber fixture missing")


def _run(check_id, design_data):
    from pcb_dfm.checks.definitions import load_check_definition
    from pcb_dfm.engine.check_runner import run_single_check
    return run_single_check(GERBER, load_check_definition(check_id), design_data=design_data)


def test_diff_pair_skew_fail_from_ipc2581():
    r = _run("diff_pair_skew", IPC)
    # CLK_P=20.0, CLK_N=21.2 -> skew 1.2 mm > 1.0 mm absolute limit -> fail.
    assert r.status == "fail"
    assert r.metric.measured_value == pytest.approx(1.2, abs=1e-6)


def test_diff_pair_skew_not_applicable_without_design_data():
    r = _run("diff_pair_skew", None)
    assert r.status == "not_applicable"


def test_impedance_from_ipc2581_stackup():
    # RF net: 50 ohm target, 0.20 mm wide, on Er=4.3 / h=0.20 mm / t=0.035 mm
    # microstrip -> ~66 ohm -> well outside 10% -> fail, computed from the
    # IPC-2581 stackup with no sidecar.
    r = _run("impedance_control", IPC)
    assert r.status == "fail"
    assert r.metric.units == "%"


_STACKUP = {"er": 4.3, "dielectric_thickness_mm": 0.20, "copper_thickness_mm": 0.035,
            "dielectric_layers_mm": [0.20, 0.20]}


def test_impedance_stripline_uses_stripline_model():
    # Same width/target as a microstrip, but geometry=stripline -> the stripline
    # formula gives a different (lower) Z0, so the two models are distinguishable.
    ms = _run("impedance_control", {"stackup": _STACKUP, "controlled_impedance": [
        {"name": "SIG", "width_mm": 0.20, "target_ohm": 50.0, "geometry": "microstrip"}]})
    sl = _run("impedance_control", {"stackup": _STACKUP, "controlled_impedance": [
        {"name": "SIG", "width_mm": 0.20, "target_ohm": 50.0, "geometry": "stripline"}]})
    # microstrip is well over 50 here, stripline is under -> different verdicts/dev.
    assert ms.metric.measured_value != pytest.approx(sl.metric.measured_value, abs=1.0)


def test_impedance_differential_pair_passes_at_target():
    # A gap makes it differential; the modelled Zdiff (~108 ohm for this
    # geometry) matches a target set to it -> pass. (Same spec without the gap is
    # single-ended ~66 ohm, so the differential path is genuinely engaged.)
    r = _run("impedance_control", {"stackup": _STACKUP, "controlled_impedance": [
        {"name": "USB", "width_mm": 0.20, "target_ohm": 108.0, "tolerance_pct": 10,
         "spacing_mm": 0.20}]})
    assert r.status == "pass"
    assert r.metric.measured_value < 5.0


def test_impedance_differential_flags_gross_mismatch():
    # A 50 ohm differential target on a wide-spaced pair is far off -> fail.
    r = _run("impedance_control", {"stackup": _STACKUP, "controlled_impedance": [
        {"name": "X", "width_mm": 0.20, "target_ohm": 50.0, "tolerance_pct": 10,
         "spacing_mm": 0.60}]})
    assert r.status == "fail"


def test_dielectric_uniformity_pass_from_ipc2581():
    # Both dielectrics are 0.20 mm -> zero deviation -> pass.
    r = _run("dielectric_thickness_uniformity", IPC)
    assert r.status == "pass"
    assert r.metric.measured_value == pytest.approx(0.0, abs=1e-6)
