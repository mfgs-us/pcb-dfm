"""
Tests for fab capability profiles (rulesets): profile resolution, check
selection, threshold overrides, and the end-to-end effect on a check verdict.
"""

import zipfile
from pathlib import Path

import pytest

from pcb_dfm.checks.definitions import (
    list_ruleset_ids,
    load_all_check_definitions,
    load_check_definitions_for_ruleset,
)

# --------------------------------------------------------------------------
# Profile resolution / selection / overrides (no Gerbers needed)
# --------------------------------------------------------------------------

def test_starter_rulesets_present():
    ids = set(list_ruleset_ids())
    assert {"default", "advanced_hdi", "conservative_2layer"} <= ids


def test_default_returns_all_checks_unchanged():
    base = load_all_check_definitions()
    default = load_check_definitions_for_ruleset("default")
    assert {d.id for d in base} == {d.id for d in default}


def test_unknown_ruleset_raises():
    with pytest.raises(KeyError):
        load_check_definitions_for_ruleset("no_such_fab")


def test_conservative_disables_high_speed_category():
    cons = load_check_definitions_for_ruleset("conservative_2layer")
    assert all(d.category_id != "high_speed_si" for d in cons)
    # default keeps them
    default = load_check_definitions_for_ruleset("default")
    assert any(d.category_id == "high_speed_si" for d in default)


def test_overrides_reach_check_definition_limits():
    by_id = {d.id: d for d in load_check_definitions_for_ruleset("conservative_2layer")}
    assert by_id["min_trace_width"].limits["absolute_min"] == pytest.approx(0.127)
    adv = {d.id: d for d in load_check_definitions_for_ruleset("advanced_hdi")}
    assert adv["min_trace_width"].limits["absolute_min"] == pytest.approx(0.075)


def test_policy_injected_into_every_check():
    # conservative sets fab_clips_silkscreen=True as a global policy flag.
    cons = load_check_definitions_for_ruleset("conservative_2layer")
    assert all(d.raw.get("fab_clips_silkscreen") is True for d in cons)


# --------------------------------------------------------------------------
# End-to-end: the SAME board flips verdict across profiles
# --------------------------------------------------------------------------

pytest.importorskip("gerber", reason="pcb-tools (gerber) not installed")


def _board_with_trace(tmp_path: Path, width_mm: float) -> Path:
    gtl = (
        "%FSLAX46Y46*%\n%MOMM*%\n"
        f"%ADD10C,{width_mm:.6f}*%\n"
        "D10*\nX1000000Y1000000D02*\nX9000000Y1000000D01*\nM02*\n"
    )
    gko = (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.100000*%\nD10*\n"
        "X0Y0D02*\nX10000000Y0D01*\nX10000000Y5000000D01*\n"
        "X0Y5000000D01*\nX0Y0D01*\nM02*\n"
    )
    zpath = tmp_path / "board.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("board.gtl", gtl)
        zf.writestr("board.gko", gko)
    return zpath


def _run_min_trace_width(zpath: Path, ruleset: str):
    from pcb_dfm.engine.check_runner import run_single_check
    d = {x.id: x for x in load_check_definitions_for_ruleset(ruleset)}["min_trace_width"]
    return run_single_check(zpath, d, ruleset_id=ruleset)


def test_same_board_flips_verdict_across_profiles(tmp_path):
    # A 0.11 mm trace is fine for fine-line HDI (abs min 0.075) but violates a
    # conservative economy process (abs min 0.127).
    zpath = _board_with_trace(tmp_path, 0.11)

    adv = _run_min_trace_width(zpath, "advanced_hdi")
    cons = _run_min_trace_width(zpath, "conservative_2layer")

    assert adv.status == "pass"
    assert cons.status == "fail"
    # Same measured geometry, different verdict -> the profile did the work.
    assert adv.metric.measured_value == pytest.approx(cons.metric.measured_value, abs=1e-6)
