"""microvia_geometry correctness.

Aspect ratio = dielectric depth (from the stackup, between the two copper layers
the via names) / drill diameter. Validated locally against the KiCad QA boards
issue22536 (proper 0.33:1 microvia -> pass) and issue18142 (a "micro" spanning
two dielectrics at 1.70:1 -> fail); those boards are GPL and not vendored, so the
committed cases below reproduce the same geometry synthetically.
"""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import DesignData, Net, Stackup, StackupLayer, Via


def _stackup() -> Stackup:
    # F.Cu / d1 0.10 / In1.Cu / d2 0.10 / In2.Cu / d3 0.10 / B.Cu
    def cu(n):
        return StackupLayer(name=n, kind="copper", thickness_mm=0.035)

    def di(n, t):
        return StackupLayer(name=n, kind="dielectric", thickness_mm=t)
    return Stackup(layers=[
        cu("F.Cu"), di("d1", 0.10), cu("In1.Cu"), di("d2", 0.10),
        cu("In2.Cu"), di("d3", 0.10), cu("B.Cu"),
    ])


def _run(vias, stackup=None):
    _ensure_impls_loaded()
    dd = DesignData(source="test")
    dd.stackup = stackup if stackup is not None else _stackup()
    dd.nets = {"N": Net(name="N", vias=list(vias))}
    ctx = CheckContext(
        check_def=load_check_definition("microvia_geometry"),
        ingest=None, geometry=BoardGeometry(root_dir=Path(".")),
        geometry_cache=GeometryCache(), ruleset_id="default", design_id="t",
        gerber_zip=Path("x"), design_data=dd)
    return get_check_runner("microvia_geometry")(ctx)


def _micro(x, drill, a="F.Cu", b="In1.Cu"):
    return Via(x_mm=x, y_mm=0.0, from_layer=a, to_layer=b, via_type="micro", drill_mm=drill)


def test_microvia_pass_shallow():
    # depth 0.10 / drill 0.30 = 0.33:1 -> pass.
    r = _run([_micro(1.0, 0.30)])
    assert r.status == "pass"
    assert abs(r.metric.measured_value - (0.10 / 0.30)) < 1e-6


def test_microvia_warns_marginal_aspect():
    # depth 0.10 / drill 0.12 = 0.83:1 -> between 0.75 and 1.0 -> warning.
    r = _run([_micro(1.0, 0.12)])
    assert r.status == "warning"
    assert abs(r.metric.measured_value - (0.10 / 0.12)) < 1e-6


def test_microvia_fails_deep_aspect():
    # depth 0.10 / drill 0.08 = 1.25:1 -> exceeds the 1.0 limit -> fail.
    r = _run([_micro(1.0, 0.08)])
    assert r.status == "fail"
    assert r.metric.measured_value > 1.0


def test_microvia_fails_multi_dielectric_span():
    # A "micro" reaching F.Cu -> In2.Cu skips In1.Cu (two dielectrics + a copper
    # layer between) -> not a single-dielectric microvia -> fail, independent of
    # the drill diameter.
    r = _run([_micro(1.0, 0.30, a="F.Cu", b="In2.Cu")])
    assert r.status == "fail"
    assert any("more than one dielectric" in v.message for v in r.violations)


def test_microvia_not_applicable_without_microvias():
    # Through + blind vias only -> not a microvia concern.
    vias = [
        Via(x_mm=0, y_mm=0, from_layer="F.Cu", to_layer="B.Cu", via_type="through"),
        Via(x_mm=1, y_mm=0, from_layer="In1.Cu", to_layer="In2.Cu",
            via_type="blind", drill_mm=0.2),
    ]
    r = _run(vias)
    assert r.status == "not_applicable"


def test_microvia_not_applicable_without_stackup():
    r = _run([_micro(1.0, 0.30)], stackup=Stackup(layers=[]))
    assert r.status == "not_applicable"
