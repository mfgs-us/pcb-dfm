"""Correctness tests for the stackup_symmetry check.

An asymmetric layer build warps on reflow. This check pairs each layer at
position k from the top with the layer at position k from the bottom and
reports the largest thickness mismatch (um); a kind mismatch is a hard fail. It
needs an ordered design-data stackup and is not_applicable from bare Gerbers.

The ordered stackup is supplied through the sidecar adapter's
``stackup.layers`` list (top-to-bottom order preserved).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check
from pcb_dfm.ingest.design_data import load_design_data

GERBER = Path("testdata/mini_board.zip")
pytestmark = pytest.mark.skipif(not GERBER.exists(), reason="gerber fixture missing")


def _cu(t: float) -> dict:
    return {"kind": "copper", "thickness_mm": t}


def _di(t: float) -> dict:
    return {"kind": "dielectric", "thickness_mm": t}


def _dd(layers: list[dict]):
    return load_design_data({"stackup": {"layers": layers}})


def _run(layers: list[dict] | None):
    dd = _dd(layers) if layers is not None else None
    return run_single_check(GERBER, load_check_definition("stackup_symmetry"), design_data=dd)


# A textbook symmetric 4-layer build: Cu / prepreg / Cu / core / Cu / prepreg / Cu.
_SYM = [_cu(0.035), _di(0.200), _cu(0.017), _di(0.700), _cu(0.017), _di(0.200), _cu(0.035)]


def test_symmetric_stackup_passes(tmp_path):
    r = _run(_SYM)
    assert r.status == "pass"
    assert r.metric.measured_value == pytest.approx(0.0)


def test_dielectric_asymmetry_fails(tmp_path):
    # Bottom prepreg 0.300 vs top 0.200 -> 100 um mismatch, above the 50 um limit.
    layers = [_cu(0.035), _di(0.200), _cu(0.017), _di(0.700), _cu(0.017), _di(0.300), _cu(0.035)]
    r = _run(layers)
    assert r.status == "fail"
    assert r.metric.measured_value == pytest.approx(100.0, abs=0.1)


def test_mild_asymmetry_warns(tmp_path):
    # 30 um mismatch: above the 20 um target, below the 50 um hard limit.
    layers = [_cu(0.035), _di(0.200), _cu(0.017), _di(0.700), _cu(0.017), _di(0.230), _cu(0.035)]
    r = _run(layers)
    assert r.status == "warning"
    assert r.metric.measured_value == pytest.approx(30.0, abs=0.1)


def test_copper_weight_asymmetry_warns(tmp_path):
    # Outer copper 35 um (top) vs 70 um (bottom) -> 35 um mismatch -> warning.
    layers = [_cu(0.035), _di(0.200), _cu(0.017), _di(0.700), _cu(0.017), _di(0.200), _cu(0.070)]
    r = _run(layers)
    assert r.status == "warning"
    assert r.metric.measured_value == pytest.approx(35.0, abs=0.1)


def test_structural_kind_mismatch_fails(tmp_path):
    # A lopsided build where a mirror pair disagrees on kind -> hard fail.
    layers = [_cu(0.035), _cu(0.035), _di(0.200), _di(0.200), _cu(0.035)]
    r = _run(layers)
    assert r.status == "fail"


def test_no_design_data_is_not_applicable(tmp_path):
    r = _run(None)
    assert r.status == "not_applicable"


def test_too_few_layers_is_not_applicable(tmp_path):
    r = _run([_cu(0.035), _di(0.200)])
    assert r.status == "not_applicable"


def test_scalar_only_sidecar_is_not_applicable(tmp_path):
    """A sidecar that carries only scalar er/thickness (for the impedance /
    dielectric checks) synthesizes a flat [copper, dielectric, dielectric, ...]
    list -- one copper on top, then every dielectric entry. That is not a
    physical stack, and must NOT be scored as a structurally asymmetric fail.
    Regression for the >= 2 copper guard.
    """
    dd = load_design_data({
        "stackup": {"copper_thickness_mm": 0.035, "dielectric_layers_mm": [0.10, 0.20, 0.20, 0.10]},
    })
    r = run_single_check(GERBER, load_check_definition("stackup_symmetry"), design_data=dd)
    assert r.status == "not_applicable"


def test_units_are_microns(tmp_path):
    r = _run(_SYM)
    assert r.metric.units == "um"
