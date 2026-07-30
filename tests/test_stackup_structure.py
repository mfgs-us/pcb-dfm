"""Correctness tests for the structural stackup checks.

These four checks analyse the *shape* of the layer stack rather than its numbers:

  * ``stackup_construction_validity`` -- is this a physically possible build?
  * ``stackup_layer_order``           -- is it ordered the way its names claim?
  * ``stackup_lamination_validity``   -- core/prepreg construction rules
  * ``lamination_cycle_count``        -- implied build class (informational)

Plus the material comparison added to ``stackup_symmetry``: a core mirrored by a
prepreg of equal thickness used to pass, because the adapters collapsed the
distinction before the check could see it.

No Gerber is needed -- a stackup is design data, so these run the check directly
against a synthetic ``DesignData`` the way ``test_microvia.py`` does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import DesignData, Net, Stackup, StackupLayer, Via


def cu(name: str, t: float = 0.035) -> StackupLayer:
    return StackupLayer(name=name, kind="copper", thickness_mm=t)


def di(name: str, t: float = 0.100, er: float | None = None,
       material: str | None = None) -> StackupLayer:
    return StackupLayer(name=name, kind="dielectric", thickness_mm=t, er=er,
                        material=material)


def run(check_id: str, layers, vias=None):
    _ensure_impls_loaded()
    dd = DesignData(source="test")
    dd.stackup = Stackup(layers=list(layers)) if layers is not None else None
    if vias:
        dd.nets = {"N": Net(name="N", vias=list(vias))}
    ctx = CheckContext(
        check_def=load_check_definition(check_id),
        ingest=None, geometry=BoardGeometry(root_dir=Path(".")),
        geometry_cache=GeometryCache(), ruleset_id="default", design_id="t",
        gerber_zip=Path("x"), design_data=dd)
    return get_check_runner(check_id)(ctx)


# A textbook 4-layer build: Cu / prepreg / Cu / core / Cu / prepreg / Cu.
FOUR_LAYER = [
    cu("F.Cu"), di("pp1", 0.100, 4.2, "prepreg"),
    cu("In1.Cu", 0.017), di("core1", 0.700, 4.4, "core"),
    cu("In2.Cu", 0.017), di("pp2", 0.100, 4.2, "prepreg"),
    cu("B.Cu"),
]


# --------------------------------------------------------------------------
# stackup_construction_validity  (#99)
# --------------------------------------------------------------------------
def test_valid_build_passes():
    r = run("stackup_construction_validity", FOUR_LAYER)
    assert r.status == "pass"
    assert r.metric.measured_value == 0.0


def test_copper_against_copper_fails():
    layers = [cu("F.Cu"), di("pp1"), cu("In1.Cu"), cu("In2.Cu"), di("pp2"), cu("B.Cu")]
    r = run("stackup_construction_validity", layers)
    assert r.status == "fail"
    assert any("both copper" in v.message for v in r.violations)


def test_adjacent_dielectrics_are_not_a_fault():
    """Two prepreg sheets between cores is ordinary construction, not a defect.

    This is the false positive the check is most likely to produce, so it is
    pinned: a build using two sheets to hit a thickness must stay clean.
    """
    layers = [
        cu("F.Cu"), di("pp1a", 0.05, 4.2, "prepreg"), di("pp1b", 0.05, 4.2, "prepreg"),
        cu("In1.Cu"), di("core1", 0.700, 4.4, "core"),
        cu("In2.Cu"), di("pp2a", 0.05, 4.2, "prepreg"), di("pp2b", 0.05, 4.2, "prepreg"),
        cu("B.Cu"),
    ]
    r = run("stackup_construction_validity", layers)
    assert r.status == "pass"


def test_stack_ending_on_dielectric_fails():
    layers = [cu("F.Cu"), di("d1"), cu("In1.Cu"), di("d2")]
    r = run("stackup_construction_validity", layers)
    assert r.status == "fail"
    assert any("ends on a dielectric" in v.message for v in r.violations)


def test_duplicate_layer_names_fail():
    layers = [cu("F.Cu"), di("pp1"), cu("F.Cu"), di("pp2"), cu("B.Cu")]
    r = run("stackup_construction_validity", layers)
    assert r.status == "fail"
    assert any("Duplicate layer name" in v.message for v in r.violations)


def test_impossible_er_fails():
    layers = [cu("F.Cu"), di("pp1", 0.1, 0.5), cu("In1.Cu"), di("pp2", 0.1, 4.2), cu("B.Cu")]
    r = run("stackup_construction_validity", layers)
    assert r.status == "fail"
    assert any("physically impossible" in v.message for v in r.violations)


def test_er_in_wrong_units_fails():
    # 4300 instead of 4.3 -- a units/scale error, not an exotic laminate.
    layers = [cu("F.Cu"), di("pp1", 0.1, 4300.0), cu("In1.Cu"), di("pp2", 0.1, 4.2), cu("B.Cu")]
    r = run("stackup_construction_validity", layers)
    assert r.status == "fail"
    assert any("plausible laminate" in v.message for v in r.violations)


def test_non_positive_thickness_fails():
    layers = [cu("F.Cu"), di("pp1", 0.0), cu("In1.Cu"), di("pp2", 0.1), cu("B.Cu")]
    r = run("stackup_construction_validity", layers)
    assert r.status == "fail"
    assert any("non-positive thickness" in v.message for v in r.violations)


def test_odd_copper_count_is_informational_only():
    layers = [cu("F.Cu"), di("pp1"), cu("In1.Cu"), di("pp2"), cu("B.Cu")]
    r = run("stackup_construction_validity", layers)
    assert r.status == "pass"           # legal, just unusual
    assert r.metric.measured_value == 0.0
    assert any("odd copper-layer count" in v.message for v in r.violations)


def test_all_copper_stack_fails():
    """The IPC-2581 no-layer-function fallback yields an all-copper stack.

    That input is what this check exists to stop the numeric checks consuming.
    """
    layers = [cu("TOP"), cu("L2"), cu("L3"), cu("BOT")]
    r = run("stackup_construction_validity", layers)
    assert r.status == "fail"
    assert sum("both copper" in v.message for v in r.violations) == 3


def test_scalar_sidecar_synthesis_is_not_applicable():
    """One copper layer followed by dielectrics is the sidecar's scalar synthesis.

    It is not a physical stack and must not be graded as one -- without this guard
    the check fails on every scalar sidecar.
    """
    layers = [cu("copper"), di("dielectric_1"), di("dielectric_2")]
    r = run("stackup_construction_validity", layers)
    assert r.status == "not_applicable"


def test_no_stackup_is_not_applicable():
    r = run("stackup_construction_validity", None)
    assert r.status == "not_applicable"


# --------------------------------------------------------------------------
# stackup_layer_order  (#100)
# --------------------------------------------------------------------------
def test_ordered_stack_passes():
    r = run("stackup_layer_order", FOUR_LAYER)
    assert r.status == "pass"
    assert r.metric.measured_value == 0.0


def test_reversed_stack_fails():
    reversed_layers = list(reversed(FOUR_LAYER))
    r = run("stackup_layer_order", reversed_layers)
    assert r.status == "fail"
    assert any("bottom-to-top" in v.message for v in r.violations)


def test_transposed_inner_layers_fail():
    layers = [
        cu("F.Cu"), di("pp1"), cu("In2.Cu"), di("core1"), cu("In1.Cu"), di("pp2"), cu("B.Cu"),
    ]
    r = run("stackup_layer_order", layers)
    assert r.status == "fail"
    assert any("numbering decreases" in v.message for v in r.violations)


def test_gap_in_inner_numbering_fails():
    layers = [
        cu("F.Cu"), di("d1"), cu("In1.Cu"), di("d2"), cu("In2.Cu"), di("d3"),
        cu("In4.Cu"), di("d4"), cu("B.Cu"),
    ]
    r = run("stackup_layer_order", layers)
    assert r.status == "fail"
    assert any("gaps" in v.message for v in r.violations)


def test_l_numbering_convention_is_understood():
    layers = [cu("L1"), di("d1"), cu("L2"), di("d2"), cu("L3"), di("d3"), cu("L4")]
    r = run("stackup_layer_order", layers)
    assert r.status == "pass"


def test_unrecognised_naming_is_not_applicable():
    layers = [cu("alpha"), di("d1"), cu("beta"), di("d2"), cu("gamma")]
    r = run("stackup_layer_order", layers)
    assert r.status == "not_applicable"


def test_two_layer_board_with_side_names_passes():
    r = run("stackup_layer_order", [cu("F.Cu"), di("core1"), cu("B.Cu")])
    assert r.status == "pass"


# --------------------------------------------------------------------------
# stackup_lamination_validity  (#101)
# --------------------------------------------------------------------------
def test_valid_lamination_passes():
    r = run("stackup_lamination_validity", FOUR_LAYER)
    assert r.status == "pass"


def test_no_material_data_is_not_applicable():
    layers = [cu("F.Cu"), di("pp1"), cu("In1.Cu"), di("core1"), cu("In2.Cu"),
              di("pp2"), cu("B.Cu")]
    r = run("stackup_lamination_validity", layers)
    assert r.status == "not_applicable"


def test_all_prepreg_build_has_no_core():
    layers = [
        cu("F.Cu"), di("pp1", 0.1, 4.2, "prepreg"), cu("In1.Cu"),
        di("pp2", 0.1, 4.2, "prepreg"), cu("B.Cu"),
    ]
    r = run("stackup_lamination_validity", layers)
    assert r.status == "fail"
    assert any("no core" in v.message for v in r.violations)


def test_core_against_core_fails():
    layers = [
        cu("F.Cu"), di("pp1", 0.1, 4.2, "prepreg"), cu("In1.Cu"),
        di("core1", 0.5, 4.4, "core"), di("core2", 0.5, 4.4, "core"),
        cu("In2.Cu"), di("pp2", 0.1, 4.2, "prepreg"), cu("B.Cu"),
    ]
    r = run("stackup_lamination_validity", layers)
    assert r.status == "fail"
    assert any("do not bond to one another" in v.message for v in r.violations)


def test_thin_prepreg_bond_between_cores_fails():
    layers = [
        cu("F.Cu"), di("pp1", 0.1, 4.2, "prepreg"), cu("In1.Cu"),
        di("core1", 0.4, 4.4, "core"), di("bond", 0.04, 4.2, "prepreg"),
        di("core2", 0.4, 4.4, "core"),
        cu("In2.Cu"), di("pp2", 0.1, 4.2, "prepreg"), cu("B.Cu"),
    ]
    r = run("stackup_lamination_validity", layers)
    assert r.status == "fail"
    assert any("prepreg bonds the cores" in v.message for v in r.violations)


def test_unknown_material_skips_the_core_rule_rather_than_failing():
    """A dielectric that states no material could be the core, so no fault."""
    layers = [
        cu("F.Cu"), di("pp1", 0.1, 4.2, "prepreg"), cu("In1.Cu"),
        di("unknown", 0.7, 4.4, None), cu("In2.Cu"),
        di("pp2", 0.1, 4.2, "prepreg"), cu("B.Cu"),
    ]
    r = run("stackup_lamination_validity", layers)
    assert r.status == "pass"
    assert any("may be the core" in v.message for v in r.violations)


# --------------------------------------------------------------------------
# lamination_cycle_count  (#102)
# --------------------------------------------------------------------------
def _via(a, b, kind="through", x=0.0):
    return Via(x_mm=x, y_mm=0.0, from_layer=a, to_layer=b, via_type=kind, drill_mm=0.3)


def test_through_hole_board_is_one_cycle():
    r = run("lamination_cycle_count", FOUR_LAYER, [_via("F.Cu", "B.Cu")])
    assert r.status == "pass"
    assert r.metric.measured_value == 1.0
    assert any("single lamination cycle" in v.message for v in r.violations)


def test_microvia_buildup_reports_one_plus_n_plus_one():
    vias = [
        _via("F.Cu", "In1.Cu", "micro", x=1.0),
        _via("B.Cu", "In2.Cu", "micro", x=2.0),
    ]
    r = run("lamination_cycle_count", FOUR_LAYER, vias)
    assert r.status == "pass"
    assert r.metric.measured_value == 2.0
    assert any("1+N+1" in v.message for v in r.violations)


def test_buried_via_implies_a_sub_lamination():
    r = run("lamination_cycle_count", FOUR_LAYER,
            [_via("In1.Cu", "In2.Cu", "buried")])
    assert r.status == "pass"
    assert r.metric.measured_value == 2.0
    assert any("sub-lamination" in v.message for v in r.violations)


def test_never_worse_than_pass():
    """Cost is not a manufacturability verdict: a deep HDI build still passes."""
    vias = [
        _via("F.Cu", "In1.Cu", "micro", x=1.0),
        _via("In1.Cu", "In2.Cu", "buried", x=2.0),
        _via("B.Cu", "In2.Cu", "micro", x=3.0),
    ]
    r = run("lamination_cycle_count", FOUR_LAYER, vias)
    assert r.status == "pass"
    assert r.severity in ("info", None)


def test_no_vias_is_not_applicable():
    r = run("lamination_cycle_count", FOUR_LAYER, None)
    assert r.status == "not_applicable"


# --------------------------------------------------------------------------
# stackup_symmetry: the material comparison (#101)
# --------------------------------------------------------------------------
def test_core_mirrored_by_prepreg_fails_even_at_equal_thickness():
    """The case that passed silently before material was carried through.

    Thicknesses mirror exactly, so the numeric measure is 0 um; the asymmetry is
    entirely in the material, and cured core shrinks differently from pressed
    prepreg.
    """
    layers = [
        cu("F.Cu"), di("core_top", 0.200, 4.4, "core"),
        cu("In1.Cu", 0.017), di("core_mid", 0.700, 4.4, "core"),
        cu("In2.Cu", 0.017), di("pp_bot", 0.200, 4.2, "prepreg"),
        cu("B.Cu"),
    ]
    r = run("stackup_symmetry", layers)
    assert r.status == "fail"
    assert r.metric.measured_value == pytest.approx(0.0)
    assert any("core" in v.message and "prepreg" in v.message for v in r.violations)


def test_symmetric_materials_still_pass():
    r = run("stackup_symmetry", FOUR_LAYER)
    assert r.status == "pass"


def test_material_comparison_only_fires_when_both_sides_state_it():
    """One known and one unknown material is not evidence of a mismatch."""
    layers = [
        cu("F.Cu"), di("pp_top", 0.200, 4.2, "prepreg"),
        cu("In1.Cu", 0.017), di("core_mid", 0.700, 4.4, "core"),
        cu("In2.Cu", 0.017), di("unknown", 0.200, 4.2, None),
        cu("B.Cu"),
    ]
    r = run("stackup_symmetry", layers)
    assert r.status == "pass"
