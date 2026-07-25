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
