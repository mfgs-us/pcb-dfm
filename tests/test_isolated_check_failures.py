"""Each of the three checks added this session is tripped by a *minimal*,
*isolated* edit to a known-clean board.

`mini_board.zip` passes all 49 checks. For each new check we apply the smallest
edit that makes it fail and assert two things:

  1. the target check goes from not-failing to ``fail``; and
  2. NO other check newly fails (isolation) -- the edit is surgical.

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


def _assert_isolated_failure(
    baseline: Dict[str, str], mutated: Dict[str, str], target: str
) -> None:
    """The target went to fail, and it is the ONLY newly-failing check."""
    assert baseline.get(target) != "fail", (
        f"{target} already failed on the baseline board; the edit proves nothing"
    )
    assert mutated.get(target) == "fail", (
        f"expected {target} to fail after the edit, got {mutated.get(target)!r}"
    )
    newly_failed = {
        cid for cid, st in mutated.items()
        if st == "fail" and baseline.get(cid) != "fail"
    }
    assert newly_failed == {target}, (
        f"edit for {target} was not isolated; newly-failing checks: "
        f"{sorted(newly_failed)}"
    )


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
