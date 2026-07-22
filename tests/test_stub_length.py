"""Routing topology + highspeed_stub_length (#11)."""

from __future__ import annotations

import math
from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset
from pcb_dfm.engine.check_runner import HEURISTIC_CHECK_IDS, get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.geometry.net_topology import max_stub_length_mm
from pcb_dfm.ingest.design_model import (
    ControlledImpedanceSpec,
    DesignData,
    Net,
    NetFeature,
    Via,
)


def _net(name, segments, vias=None):
    return Net(
        name=name,
        features=[NetFeature(layer="F.Cu", segments=list(segments),
                             length_mm=sum(math.dist(a, b) for a, b in segments))],
        vias=list(vias or []),
    )


# --- topology ---------------------------------------------------------------

def test_point_to_point_has_no_stub():
    n = _net("HS", [((0, 0), (10, 0))])
    assert max_stub_length_mm(n) == 0.0


def test_tee_branch_is_a_stub():
    # Trunk 0->10 on x; a 3 mm branch tees off at x=5 going up in y.
    n = _net("HS", [((0, 0), (5, 0)), ((5, 0), (10, 0)), ((5, 0), (5, 3))])
    assert math.isclose(max_stub_length_mm(n), 3.0, abs_tol=1e-9)


def test_via_terminated_branch_is_not_a_stub():
    # Same tee, but the branch end sits on a via -> legitimate layer transition.
    n = _net("HS",
             [((0, 0), (5, 0)), ((5, 0), (10, 0)), ((5, 0), (5, 3))],
             vias=[Via(x_mm=5.0, y_mm=3.0, from_layer="F.Cu", to_layer="B.Cu")])
    assert max_stub_length_mm(n) == 0.0


def test_no_geometry_returns_none():
    assert max_stub_length_mm(Net(name="X")) is None


def test_loop_is_skipped():
    # A square loop has no clean trunk/stub decomposition -> None.
    n = _net("HS", [((0, 0), (10, 0)), ((10, 0), (10, 10)),
                    ((10, 10), (0, 10)), ((0, 10), (0, 0))])
    assert max_stub_length_mm(n) is None


# --- check ------------------------------------------------------------------

def _run(dd):
    _ensure_impls_loaded()
    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}["highspeed_stub_length"]
    ctx = CheckContext(
        check_def=cdef, ingest=None, geometry=BoardGeometry(root_dir=Path(".")),
        geometry_cache=GeometryCache(), ruleset_id="default", design_id="t",
        gerber_zip=Path("x"), design_data=dd,
    )
    return get_check_runner("highspeed_stub_length")(ctx)


def _measured(r):
    m = r.metric
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


def _hs_design(net):
    dd = DesignData(source="test")
    dd.nets = {net.name: net}
    dd.controlled_impedance = [ControlledImpedanceSpec(name=net.name, target_ohm=50)]
    return dd


def test_check_flags_long_stub():
    # Trunk 0->30 mm; a 12 mm stub tees off at x=15 (stub shorter than each
    # trunk arm, as real stubs are). 12 mm > 10 mm limit -> fail.
    n = _net("HS", [((0, 0), (15, 0)), ((15, 0), (30, 0)), ((15, 0), (15, 12))])
    r = _run(_hs_design(n))
    assert r.status == "fail"
    assert math.isclose(_measured(r), 12.0, abs_tol=1e-9)
    assert r.violations and r.violations[0].location.net == "HS"


def test_check_clean_route_passes():
    n = _net("HS", [((0, 0), (20, 0))])
    r = _run(_hs_design(n))
    assert r.status == "pass"
    assert _measured(r) == 0.0


def test_check_not_applicable_without_high_speed_nets():
    dd = DesignData(source="test")
    dd.nets = {"GND": _net("GND", [((0, 0), (5, 0)), ((5, 0), (5, 9))])}
    assert _run(dd).status == "not_applicable"


def test_check_not_applicable_without_design_data():
    assert _run(None).status == "not_applicable"


def test_labeled_heuristic():
    assert "highspeed_stub_length" in HEURISTIC_CHECK_IDS
