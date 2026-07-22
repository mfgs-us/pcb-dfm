"""crosstalk_estimate: first-order coupling risk between sensitive nets."""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset
from pcb_dfm.engine.check_runner import HEURISTIC_CHECK_IDS, get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import (
    ControlledImpedanceSpec,
    DesignData,
    DiffPair,
    Net,
    NetFeature,
    Stackup,
    StackupLayer,
)


def _stackup(h=0.2):
    return Stackup(layers=[
        StackupLayer("F.Cu", "copper", 0.035),
        StackupLayer("d1", "dielectric", h, 4.5),
        StackupLayer("B.Cu", "copper", 0.035),
    ])


def _net(name, seg, width=0.1, layer="F.Cu"):
    return Net(name=name, features=[
        NetFeature(layer=layer, length_mm=20, width_mm=width, segments=[seg]),
    ])


def _ctx(dd):
    _ensure_impls_loaded()
    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}["crosstalk_estimate"]
    return CheckContext(
        check_def=cdef, ingest=None, geometry=BoardGeometry(root_dir=Path(".")),
        geometry_cache=GeometryCache(), ruleset_id="default", design_id="t",
        gerber_zip=Path("x"), design_data=dd,
    )


def _run(dd):
    ctx = _ctx(dd)   # builds ctx (and loads impls) before resolving the runner
    return get_check_runner("crosstalk_estimate")(ctx)


def _measured(r):
    m = r.metric
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


def test_close_parallel_sensitive_nets_are_flagged():
    dd = DesignData(source="test", stackup=_stackup(0.2))
    dd.nets = {
        "HS1": _net("HS1", ((0, 0.0), (20, 0.0))),
        "HS2": _net("HS2", ((0, 0.2), (20, 0.2))),   # 0.1 mm edge gap, 20 mm parallel
    }
    dd.controlled_impedance = [
        ControlledImpedanceSpec(name="HS1", target_ohm=50),
        ControlledImpedanceSpec(name="HS2", target_ohm=50),
    ]
    r = _run(dd)
    assert r.status in ("warning", "fail")
    assert _measured(r) > 25.0                       # strong coupling: close, long, thin dielectric
    assert r.violations and "HS1" in r.violations[0].message


def test_well_separated_nets_pass():
    dd = DesignData(source="test", stackup=_stackup(0.2))
    dd.nets = {
        "HS1": _net("HS1", ((0, 0.0), (20, 0.0))),
        "HS2": _net("HS2", ((0, 3.0), (20, 3.0))),   # 2.9 mm edge gap
    }
    dd.controlled_impedance = [
        ControlledImpedanceSpec(name="HS1", target_ohm=50),
        ControlledImpedanceSpec(name="HS2", target_ohm=50),
    ]
    r = _run(dd)
    assert r.status == "pass"
    assert _measured(r) < 10.0


def test_same_diff_pair_partners_excluded():
    # A single diff pair, tightly coupled by design -> not crosstalk -> N/A.
    dd = DesignData(source="test", stackup=_stackup(0.2))
    dd.nets = {
        "USB_DP": _net("USB_DP", ((0, 0.0), (20, 0.0))),
        "USB_DN": _net("USB_DN", ((0, 0.2), (20, 0.2))),
    }
    dd.diff_pairs = [DiffPair(name="USB", positive="USB_DP", negative="USB_DN")]
    r = _run(dd)
    assert r.status == "not_applicable"


def test_no_design_data_is_not_applicable():
    ctx = _ctx(None)
    assert get_check_runner("crosstalk_estimate")(ctx).status == "not_applicable"


def test_single_high_speed_net_is_not_applicable():
    dd = DesignData(source="test", stackup=_stackup())
    dd.nets = {"HS1": _net("HS1", ((0, 0.0), (20, 0.0)))}
    dd.controlled_impedance = [ControlledImpedanceSpec(name="HS1", target_ohm=50)]
    assert _run(dd).status == "not_applicable"


def test_labeled_heuristic():
    assert "crosstalk_estimate" in HEURISTIC_CHECK_IDS
