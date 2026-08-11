"""Each covered check is tripped by a *minimal*, *isolated* edit to a known-clean
board.

`mini_board.zip` passes every check. For each covered check we apply the
smallest edit that trips it and assert two things:

  1. the target check reaches its worst status (``fail``, or ``warning`` for an
     advisory check that never fails); and
  2. NO other check newly *fails* (isolation) -- the edit is surgical. A surgical
     edit may nudge neighbouring advisories to ``warning``; only a new hard
     failure breaks isolation.

The isolation criterion compares against a baseline run: a check that was
already failing (or is unaffected) does not count; only a *new* failure
introduced by the edit does. That keeps the test honest even if the base board
ever develops a pre-existing failure.

Edits used:
  * board_outline_continuity -- delete the outline's closing segment so the
    profile no longer forms a closed loop. The remaining three segments still
    touch all four bbox extremes, so board bounds (and every bounds-dependent
    check) are unchanged.
  * npth_to_copper_clearance -- add a non-plated drill file with a hole sitting
    on the wide signal trace (a large-extent copper feature, so it is not
    mistaken for the hole's own ring) -> zero clearance.
  * stackup_symmetry -- compare a symmetric stackup sidecar against the same
    stackup with a single copper weight bumped 35 -> 105 um. Every other
    stackup-consuming check sees a near-identical build in both runs, so the
    70 um mid-plane mismatch is the only thing that changes.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Dict

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.engine.run import run_dfm_on_gerber_zip
from pcb_dfm.ingest.design_data import load_design_data

BASE = Path("testdata/mini_board.zip")
pytestmark = pytest.mark.skipif(not BASE.exists(), reason="mini_board fixture missing")


def _base_files() -> Dict[str, bytes]:
    with zipfile.ZipFile(BASE) as z:
        return {n: z.read(n) for n in z.namelist()}


def _write_zip(tmp_path: Path, files: Dict[str, bytes]) -> Path:
    p = tmp_path / "mutated.zip"
    with zipfile.ZipFile(p, "w") as z:
        for name, blob in files.items():
            z.writestr(name, blob)
    return p


def _status_map(zip_path: Path, design_data=None) -> Dict[str, str]:
    res = run_dfm_on_gerber_zip(zip_path, "default", design_data=design_data)
    return {c.check_id: c.status for cat in res.categories for c in cat.checks}


def _replace_in(files: Dict[str, bytes], name: str, old: str, new: str, count: int = 1) -> None:
    """Edit a text layer in the in-memory package (latin-1 preserves Gerber bytes)."""
    text = files[name].decode("latin-1")
    assert old in text, f"{old!r} not found in {name}"
    files[name] = text.replace(old, new, count).encode("latin-1")


def _mutated_map(tmp_path: Path, mutate) -> Dict[str, str]:
    """Run the full suite on mini_board after applying an in-place file edit."""
    files = _base_files()
    mutate(files)
    return _status_map(_write_zip(tmp_path, files))


def _assert_isolated_regression(
    baseline: Dict[str, str], mutated: Dict[str, str], target: str, worst: str = "fail"
) -> None:
    """The target reached its worst status, and the edit introduced no unrelated
    hard failure.

    ``worst`` is the status the target should reach: ``"fail"`` for a hard-reject
    check, ``"warning"`` for an advisory one (which never fails). Isolation is
    always measured against *fail*: a surgical edit may nudge neighbouring
    advisories to ``warning`` (adding copper trips density/floating-copper, say),
    but it must introduce no new *fail* other than the target itself.
    """
    assert baseline.get(target) != worst, (
        f"{target} already {worst} on the baseline board; the edit proves nothing"
    )
    assert mutated.get(target) == worst, (
        f"expected {target} to reach {worst!r} after the edit, "
        f"got {mutated.get(target)!r}"
    )
    newly_failed = {
        cid for cid, st in mutated.items()
        if st == "fail" and baseline.get(cid) != "fail"
    }
    allowed = {target} if worst == "fail" else set()
    assert newly_failed <= allowed, (
        f"edit for {target} was not isolated; unrelated newly-failing checks: "
        f"{sorted(newly_failed - allowed)}"
    )


def _assert_isolated_failure(
    baseline: Dict[str, str], mutated: Dict[str, str], target: str
) -> None:
    """The target went to fail, and it is the ONLY newly-failing check."""
    _assert_isolated_regression(baseline, mutated, target, worst="fail")


def test_board_outline_continuity_isolated(tmp_path):
    files = _base_files()
    gko = files["board.gko"].decode("latin-1")
    # Drop the segment that draws back to the origin (X0Y0D01*), leaving the
    # profile open. The D02 *move* to the origin stays, so this removes exactly
    # the closing edge and nothing else.
    mutated_gko = gko.replace("X0Y0D01*\n", "", 1)
    assert "X0Y0D01*" not in mutated_gko and mutated_gko != gko
    files["board.gko"] = mutated_gko.encode("latin-1")

    baseline = _status_map(BASE)
    mutated = _status_map(_write_zip(tmp_path, files))
    _assert_isolated_failure(baseline, mutated, "board_outline_continuity")


def test_npth_to_copper_clearance_isolated(tmp_path):
    files = _base_files()
    # A 2 mm non-plated hole centred on the signal trace, which runs from
    # (1,1) to (8,1). The trace's bbox extent (7 mm) exceeds the own-ring
    # threshold, so the bare hole through it reads as a real clearance defect.
    # Placed well away from the plated holes at (3,8)/(12,8)/(9,3).
    npth = "M48\nMETRIC,TZ\nT1C2.000\n%\nT1\nX4.0Y1.0\nT0\nM30\n"
    files["board-NPTH.drl"] = npth.encode("latin-1")

    baseline = _status_map(BASE)
    mutated = _status_map(_write_zip(tmp_path, files))
    _assert_isolated_failure(baseline, mutated, "npth_to_copper_clearance")


def test_stackup_symmetry_isolated(tmp_path):
    def cu(t: float) -> dict:
        return {"kind": "copper", "thickness_mm": t}

    def di(t: float) -> dict:
        return {"kind": "dielectric", "thickness_mm": t}

    symmetric = [
        cu(0.035), di(0.200), cu(0.035), di(0.700), cu(0.035), di(0.200), cu(0.035),
    ]
    # The single edit: bump the bottom copper weight 35 -> 105 um. Its mirror
    # partner (top copper, 35 um) now differs by 70 um -> above the 50 um limit.
    asymmetric = list(symmetric)
    asymmetric[-1] = cu(0.105)

    dd_symmetric = load_design_data({"stackup": {"layers": symmetric}})
    dd_asymmetric = load_design_data({"stackup": {"layers": asymmetric}})

    baseline = _status_map(BASE, design_data=dd_symmetric)
    mutated = _status_map(BASE, design_data=dd_asymmetric)
    _assert_isolated_failure(baseline, mutated, "stackup_symmetry")


# ==========================================================================
# Further checks -- minimal geometry edits to mini_board, each isolated.
#
# On a minimal board some checks are physically inseparable (a sub-minimum
# trace IS a copper sliver AND sits at the etch floor; a sub-minimum drill has
# an unavoidably high aspect ratio) and cannot be tripped alone -- those are
# intentionally not covered here rather than asserted with a contrived board.
# ==========================================================================


def test_copper_to_edge_distance_isolated(tmp_path):
    # Flash a 0.5 mm round pad centred at x=0.15 mm -> its copper spills over the
    # board edge at x=0. Far from other copper, so nothing else is disturbed.
    def mutate(f):
        _replace_in(f, "board.gtl", "M02*", "D12*\nX150000Y6000000D03*\nM02*")

    _assert_isolated_failure(_status_map(BASE), _mutated_map(tmp_path, mutate),
                             "copper_to_edge_distance")


def test_min_annular_ring_isolated(tmp_path):
    # Enlarge the plated drill under the 1 mm pad at (3,8) from 0.3 -> 0.95 mm,
    # leaving a 0.025 mm ring. Still a large hole (min_drill/aspect unaffected)
    # and away from its neighbour, so only the annular ring collapses.
    def mutate(f):
        _replace_in(f, "board.drl", "T1C0.300", "T1C0.950")

    _assert_isolated_failure(_status_map(BASE), _mutated_map(tmp_path, mutate),
                             "min_annular_ring")


def test_drill_to_drill_spacing_isolated(tmp_path):
    # Add a second hole 0.15 mm from the standalone 0.8 mm hole at (9,3).
    def mutate(f):
        _replace_in(f, "board.drl", "X9.0Y3.0", "X9.0Y3.0\nX9.15Y3.0")

    _assert_isolated_failure(_status_map(BASE), _mutated_map(tmp_path, mutate),
                             "drill_to_drill_spacing")


def test_min_slot_width_isolated(tmp_path):
    # Add a 0.5 mm-wide routed slot (G85) in empty board space -- below the
    # minimum routing tool.
    def mutate(f):
        _replace_in(f, "board.drl", "T2C0.800", "T2C0.800\nT3C0.500")
        _replace_in(f, "board.drl", "T0\nM30", "T3\nX14.0Y3.0G85X16.0Y3.0\nT0\nM30")

    _assert_isolated_failure(_status_map(BASE), _mutated_map(tmp_path, mutate),
                             "min_slot_width")


def test_min_trace_spacing_isolated(tmp_path):
    # Two parallel 0.25 mm traces in an empty region (y~11) with a 0.05 mm gap.
    # Kept clear of the existing trace so they read as two distinct conductors
    # rather than merging into one.
    def mutate(f):
        extra = (
            "D10*\n"
            "X2000000Y11000000D02*\nX7000000Y11000000D01*\n"
            "X2000000Y11300000D02*\nX7000000Y11300000D01*\n"
        )
        _replace_in(f, "board.gtl", "M02*", extra + "M02*")

    _assert_isolated_failure(_status_map(BASE), _mutated_map(tmp_path, mutate),
                             "min_trace_spacing")


def test_solder_mask_web_isolated(tmp_path):
    # Two 0.4 mm mask openings 0.44 mm apart -> ~0.04 mm web between them.
    # solder_mask_web is advisory (a thin web needs adjacent-net data to be a
    # confirmed bridging defect), so the edit trips it to a *warning*, and it
    # must still be surgical: nothing else newly warns or fails.
    def mutate(f):
        _replace_in(
            f, "board.gts", "M02*",
            "%ADD30R,0.400000X0.400000*%\nD30*\n"
            "X8000000Y3000000D03*\nX8440000Y3000000D03*\nM02*",
        )

    baseline = _status_map(BASE)
    mutated = _mutated_map(tmp_path, mutate)
    assert baseline.get("solder_mask_web") == "pass"
    assert mutated.get("solder_mask_web") == "warning"
    newly_flagged = {
        cid for cid, st in mutated.items()
        if st in ("warning", "fail") and baseline.get(cid) not in ("warning", "fail")
    }
    assert newly_flagged == {"solder_mask_web"}, (
        f"edit was not isolated; newly-flagged: {sorted(newly_flagged)}"
    )


def test_silkscreen_clearance_isolated(tmp_path):
    # Redraw the silk line straight across the exposed pad at (3,8): the legend
    # now sits hard against copper. (This trips silkscreen_clearance to fail;
    # silkscreen_on_copper only warns, so the failure is still isolated.)
    def mutate(f):
        _replace_in(
            f, "board.gto",
            "X2000000Y10000000D02*\nX6000000Y10000000D01*",
            "X2000000Y8000000D02*\nX4000000Y8000000D01*",
        )

    _assert_isolated_failure(_status_map(BASE), _mutated_map(tmp_path, mutate),
                             "silkscreen_clearance")


# ==========================================================================
# Data-gated checks -- the "edit" is to the design-data sidecar, compared
# against a clean baseline sidecar so that merely *supplying* design data
# (which activates several checks at once) is not what trips the target.
# ==========================================================================


def test_dielectric_thickness_uniformity_isolated(tmp_path):
    def cu(t: float) -> dict:
        return {"kind": "copper", "thickness_mm": t}

    def di(t: float) -> dict:
        return {"kind": "dielectric", "thickness_mm": t}

    # Both stackups are mirror-symmetric (so stackup_symmetry stays pass); only
    # the dielectric uniformity differs. Uniform 0.20 mm cores vs a symmetric but
    # non-uniform 0.10 / 0.30 / 0.10 set -> large deviation from the mean.
    uniform = [cu(.035), di(.20), cu(.035), di(.20), cu(.035), di(.20), cu(.035)]
    nonuniform = [cu(.035), di(.10), cu(.035), di(.30), cu(.035), di(.10), cu(.035)]

    baseline = _status_map(BASE, design_data=load_design_data({"stackup": {"layers": uniform}}))
    mutated = _status_map(BASE, design_data=load_design_data({"stackup": {"layers": nonuniform}}))
    _assert_isolated_failure(baseline, mutated, "dielectric_thickness_uniformity")


def test_diff_pair_skew_isolated(tmp_path):
    def payload(len_p: float, len_n: float) -> dict:
        return {
            "nets": {
                "CLK_P": {"routed_length_mm": len_p},
                "CLK_N": {"routed_length_mm": len_n},
            },
            "diff_pairs": [{"positive": "CLK_P", "negative": "CLK_N"}],
        }

    # Matched lengths pass; a 1.5 mm length mismatch on the same pair fails skew
    # and nothing else (no routed geometry, so diff_pair_spacing stays N/A).
    baseline = _status_map(BASE, design_data=load_design_data(payload(20.0, 20.0)))
    mutated = _status_map(BASE, design_data=load_design_data(payload(20.0, 21.5)))
    _assert_isolated_failure(baseline, mutated, "diff_pair_skew")


# --------------------------------------------------------------------------
# #23 -- data-gated checks that need routed geometry / a stackup.
#
# The baseline sidecar makes the target *active and passing*; the mutated
# sidecar changes one field to trip it, so activation (which turns on several
# design-data checks at once) is never what the assertion catches.
#
# Every number below is calibrated to h = 0.10 mm, which is what the adapter
# actually resolves: given a `dielectric_layers_mm` list it ignores any scalar
# `dielectric_thickness_mm` and Stackup.dielectric_thickness_mm returns the first
# entry. A uniform list keeps that honest and keeps the baseline clean --
# [0.10, 0.20, 0.20, 0.10] is +-33% off its own mean, which hard-failed
# dielectric_thickness_uniformity on the *baseline* run and left that check
# permanently masked by the already-failing filter.
#
# At h = 0.10 the real Zo is ~28 ohm, so the controlled-impedance targets are set
# to 28 to keep impedance_control passing where it is not the target. Note that
# `tolerance_pct` does NOT gate that pass: the status comes from the check
# definition's own target (8%) and limit (10%) on worst deviation. tolerance_pct
# only decides whether a per-net violation message is attached.
#
# Geometry stays inside mini_board's 18 x 12 mm outline. Nothing consumes that
# today, but outline-aware net checks are being added and a fixture that routes
# off the edge would start failing for reasons unrelated to its target.
# --------------------------------------------------------------------------
_STACKUP = {"er": 4.3, "copper_thickness_mm": 0.035,
            "dielectric_layers_mm": [0.10, 0.10, 0.10, 0.10]}


def test_impedance_control_isolated():
    # One controlled-impedance net, no routed segments -> only impedance_control
    # is active. Baseline width lands on target Zo; the mutation widens the trace
    # so the estimated Z falls far outside tolerance.
    def sidecar(width_mm: float) -> dict:
        return {"stackup": _STACKUP,
                "controlled_impedance": [{"name": "RF", "target_ohm": 28,
                                          "width_mm": width_mm, "tolerance_pct": 20}]}
    # 0.30 mm -> 28.3 ohm (1% off). 0.45 mm -> 15.1 ohm (46% off), a fail with
    # real margin. Deliberately NOT a wider trace: the IPC-2141 ln() term goes to
    # zero at w ~ 0.705 and negative past it, so a mutation out there asserts a
    # failure produced by model extrapolation rather than by an impedance error.
    baseline = _status_map(BASE, design_data=load_design_data(sidecar(0.30)))
    mutated = _status_map(BASE, design_data=load_design_data(sidecar(0.45)))
    _assert_isolated_failure(baseline, mutated, "impedance_control")


def test_highspeed_stub_length_isolated():
    # A high-speed net (controlled-impedance) as a trunk with one branch off its
    # middle. The trunk is the graph diameter, so the two arms (13 mm each) stay
    # longer than the branch -- otherwise the branch becomes the trunk and the
    # arms become the stubs. Only the branch length changes between runs; it
    # turns the corner at (9, 11) to reach 12 mm inside an 18 x 12 mm board.
    def sidecar(branch: list) -> dict:
        return {"stackup": _STACKUP,
                "controlled_impedance": [{"name": "HS", "target_ohm": 28,
                                          "width_mm": 0.30, "tolerance_pct": 20}],
                "nets": {"HS": {"width_mm": 0.30,
                                "segments": [[[9, 6], [1, 6]], [[1, 6], [1, 1]],
                                             [[9, 6], [17, 6]], [[17, 6], [17, 1]],
                                             *branch]}}}
    short = [[[9, 6], [9, 10]]]                                    # 4 mm
    long_ = [[[9, 6], [9, 11]], [[9, 11], [16, 11]]]               # 5 + 7 = 12 mm
    baseline = _status_map(BASE, design_data=load_design_data(sidecar(short)))
    mutated = _status_map(BASE, design_data=load_design_data(sidecar(long_)))
    _assert_isolated_failure(baseline, mutated, "highspeed_stub_length")


def test_crosstalk_estimate_isolated():
    # Two independent high-speed nets (not a diff pair, so crosstalk applies)
    # running parallel for 16 mm. Only the spacing changes.
    #
    # The nets declare width_mm so the check sees real copper: without it the
    # segments are zero-width, the "edge-to-edge spacing" it reports is really
    # centreline spacing, and the fixture describes two 0.30 mm traces that would
    # physically overlap. Gaps below are centreline; subtract 0.30 for the edge.
    def sidecar(gap_mm: float) -> dict:
        return {"stackup": _STACKUP,
                "controlled_impedance": [
                    {"name": "AGG", "target_ohm": 28, "width_mm": 0.30, "tolerance_pct": 20},
                    {"name": "VIC", "target_ohm": 28, "width_mm": 0.30, "tolerance_pct": 20}],
                "nets": {"AGG": {"width_mm": 0.30,
                                 "segments": [[[1, 3], [17, 3]]]},
                         "VIC": {"width_mm": 0.30,
                                 "segments": [[[1, 3 + gap_mm], [17, 3 + gap_mm]]]}}}
    # Both gaps are inside the 1 mm capture window, so the baseline exercises the
    # coupling falloff rather than passing by falling outside it: 0.5 mm edge
    # spacing is ~2% risk, 0.1 mm edge is ~32% (limit 25%).
    baseline = _status_map(BASE, design_data=load_design_data(sidecar(0.8)))
    mutated = _status_map(BASE, design_data=load_design_data(sidecar(0.4)))
    _assert_isolated_failure(baseline, mutated, "crosstalk_estimate")


def test_diff_pair_spacing_isolated():
    # diff_pair_spacing measures the *variation* of the intra-pair gap, not its
    # absolute value: a constant gap passes, a gap that steps mid-run fails.
    # (The step is a ~5.7 degree diagonal, not a right angle -- verified that no
    # other check changes status, advisory or otherwise.)
    def sidecar(n_segments: list) -> dict:
        return {"nets": {"DP_P": {"width_mm": 0.20,
                                  "segments": [[[1, 3], [17, 3]]]},
                         "DP_N": {"width_mm": 0.20, "segments": n_segments}},
                "diff_pairs": [{"positive": "DP_P", "negative": "DP_N", "target_ohm": 100}]}
    flat = [[[1, 3.2], [17, 3.2]]]                                   # constant 0.2 mm gap
    stepped = [[[1, 3.2], [9, 3.2]], [[9, 3.2], [11, 3.4]], [[11, 3.4], [17, 3.4]]]  # 0.2 -> 0.4
    baseline = _status_map(BASE, design_data=load_design_data(sidecar(flat)))
    mutated = _status_map(BASE, design_data=load_design_data(sidecar(stepped)))
    _assert_isolated_failure(baseline, mutated, "diff_pair_spacing")


# Deferred to #24 (the richer base fixture):
#   * return_path_interruptions -- needs a reference-plane pour with a slot the
#     trace crosses, on >= 2 copper layers. That plane geometry is not
#     expressible through the JSON sidecar (no fill-region field), so it needs a
#     built board, not a sidecar mutation.
#   * component_to_component_spacing -- clusters *Gerber* pad openings and
#     measures the gap between them; design data only labels the clusters, so a
#     sidecar/placement mutation cannot move mini_board's fixed ~9 mm pad gap. It
#     is geometry-gated, not data-gated -- a placement-carrying base fixture
#     (#24) is the right home.


# --------------------------------------------------------------------------
# #22 -- geometry-trippable checks (a single minimal edit to mini_board).
#
# Each edit injects one physical defect and asserts the target reaches its worst
# status with no unrelated hard failure. The two advisory targets (density,
# silk-on-copper) never fail, so their worst is "warning"; adding copper to trip
# them also nudges floating_copper / density, which is allowed (no new *fail*).
# --------------------------------------------------------------------------
def test_acid_trap_angle_isolated(tmp_path):
    # Flash a concave "arrowhead" aperture: its inner notch is an acute concave
    # copper corner where etchant is trapped. (A stroked trace can't do this --
    # the round aperture rounds every inner corner.)
    def mutate(f):
        _replace_in(f, "board.gtl", "%ADD12C,0.500000*%",
                    "%ADD12C,0.500000*%\n%AMDART*\n4,1,4,0,0,3,1.2,0.6,0,3,-1.2,0,0,0*\n%\n%ADD33DART*%")
        _replace_in(f, "board.gtl", "M02*", "D33*\nX13000000Y6000000D03*\nM02*")
    _assert_isolated_regression(_status_map(BASE), _mutated_map(tmp_path, mutate),
                                "acid_trap_angle")


def test_fillet_radius_milling_isolated(tmp_path):
    # Cut a rectangular notch into the top edge of the outline: its two inner
    # corners are sharp concave (zero-radius) corners a router bit can't cut.
    def mutate(f):
        _replace_in(f, "board.gko",
                    "X18000000Y12000000D01*\nX0Y12000000D01*",
                    "X18000000Y12000000D01*\nX9000000Y12000000D01*\nX9000000Y10000000D01*\n"
                    "X8000000Y10000000D01*\nX8000000Y12000000D01*\nX0Y12000000D01*")
    _assert_isolated_regression(_status_map(BASE), _mutated_map(tmp_path, mutate),
                                "fillet_radius_milling")


def test_mask_to_trace_clearance_isolated(tmp_path):
    # Add a solder-mask opening whose lower edge sits ~5 um from the top edge of
    # the horizontal signal trace at y=1 -> mask hard against a trace.
    def mutate(f):
        _replace_in(f, "board.gts", "M02*", "X5000000Y1730000D03*\nM02*")
    _assert_isolated_regression(_status_map(BASE), _mutated_map(tmp_path, mutate),
                                "mask_to_trace_clearance")


def test_copper_density_balance_isolated(tmp_path):
    # A large copper pour in one region imbalances copper across the board. The
    # check is advisory in the default profile (fails only in strict-plating
    # mode), so the worst reachable status is "warning".
    def mutate(f):
        _replace_in(f, "board.gtl", "%ADD12C,0.500000*%",
                    "%ADD12C,0.500000*%\n%ADD30R,10.000000X6.000000*%")
        _replace_in(f, "board.gtl", "M02*", "D30*\nX11000000Y4000000D03*\nM02*")
    _assert_isolated_regression(_status_map(BASE), _mutated_map(tmp_path, mutate),
                                "copper_density_balance", worst="warning")


def test_silkscreen_on_copper_isolated(tmp_path):
    # A large exposed-copper region (copper flash + a same-size mask opening) with
    # a silk line across its centre -> legend printed on bare copper. The centre
    # keeps the silk clear of the opening edges, so silkscreen_clearance stays
    # clean and only silkscreen_on_copper (advisory -> warning) trips.
    def mutate(f):
        _replace_in(f, "board.gtl", "%ADD12C,0.500000*%",
                    "%ADD12C,0.500000*%\n%ADD31R,6.000000X5.000000*%")
        _replace_in(f, "board.gtl", "M02*", "D31*\nX13000000Y4500000D03*\nM02*")
        _replace_in(f, "board.gts", "%ADD11R,1.200000X1.200000*%",
                    "%ADD11R,1.200000X1.200000*%\n%ADD32R,6.000000X5.000000*%")
        _replace_in(f, "board.gts", "M02*", "D32*\nX13000000Y4500000D03*\nM02*")
        _replace_in(f, "board.gto", "X2000000Y10000000D02*\nX6000000Y10000000D01*",
                    "X12000000Y4500000D02*\nX14000000Y4500000D01*")
    _assert_isolated_regression(_status_map(BASE), _mutated_map(tmp_path, mutate),
                                "silkscreen_on_copper", worst="warning")


# Deferred: silkscreen_over_mask_defined_pads. On mini_board it reaches "fail"
# only with placement data (a BOM/netlist confirming the copper is a real
# component pad); without it the check is advisory and, on a bare exposed pad,
# its bbox-overlap estimate did not register the silk overlap that
# silkscreen_on_copper flags. Isolating it belongs with the placement-carrying
# base fixture in #24, not a Gerber-only edit here.
