"""Correctness tests for the npth_to_copper_clearance check.

A non-plated hole (mounting/tooling hole) has no plated barrel; copper that
comes up to or into it can be nicked by the drill or short to a standoff. This
check is the companion to via_to_copper_clearance, which filters to *plated*
drills. It must:

  * FAIL when a bare hole is drilled through / hard against a large copper region
    (pour or long trace) within the absolute limit.
  * WARN when copper is nearer than the recommended keep-out but not at the hard
    limit.
  * PASS when copper is comfortably clear.
  * PASS (own-ring exclusion) when the only copper touching the hole is a small
    pad-sized feature -- an intentional grounding ring we must not false-flag.
  * be not_applicable when there is no non-plated drill layer at all (a combined
    plated drill file does not count).

Non-plated drill files are recognised by an NPTH token in the filename (see
ingest._classify_layer); artwork/format notes match test_check_correctness.py.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check


def _make_zip(tmp_path: Path, files: dict[str, str]) -> Path:
    zip_path = tmp_path / "board.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    return zip_path


def _outline() -> str:
    return (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.010000*%\nD10*\n"
        "X0Y0D02*\nX20000000Y0D01*\nX20000000Y20000000D01*\n"
        "X0Y20000000D01*\nX0Y0D01*\nM02*\n"
    )


def _copper_fill(x0: float, y0: float, x1: float, y1: float) -> str:
    """A filled copper rectangle (G36/G37 region)."""
    def t(v: float) -> int:
        return int(round(v * 1e6))
    return (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.010000*%\nD10*\nG36*\n"
        f"X{t(x0)}Y{t(y0)}D02*\nX{t(x1)}Y{t(y0)}D01*\n"
        f"X{t(x1)}Y{t(y1)}D01*\nX{t(x0)}Y{t(y1)}D01*\n"
        f"X{t(x0)}Y{t(y0)}D01*\nG37*\nM02*\n"
    )


def _npth(cx: float, cy: float, dia_mm: float) -> str:
    return f"M48\nMETRIC,TZ\nT1C{dia_mm:.3f}\n%\nT1\nX{cx:.3f}Y{cy:.3f}\nT0\nM30\n"


# A large ground pour occupying x,y = 5..15 mm (10 mm square -> extent > own-ring).
_POUR = _copper_fill(5, 5, 15, 15)


def _run(tmp_path: Path, copper: str, drill_name: str, drill: str):
    z = _make_zip(tmp_path, {"board.gtl": copper, "board.gko": _outline(), drill_name: drill})
    return run_single_check(z, load_check_definition("npth_to_copper_clearance"))


def test_hole_through_pour_fails(tmp_path):
    # 3 mm hole centred inside the pour -> drilled straight through copper.
    r = _run(tmp_path, _POUR, "board-NPTH.drl", _npth(10, 10, 3.0))
    assert r.status == "fail"
    assert r.metric.measured_value == pytest.approx(0.0)


def test_hole_edge_into_pour_fails(tmp_path):
    # 3 mm hole (r=1.5) centred at x=4.0 -> edge reaches x=5.5, into the pour at x=5.
    r = _run(tmp_path, _POUR, "board-NPTH.drl", _npth(4.0, 10, 3.0))
    assert r.status == "fail"
    assert r.metric.measured_value == pytest.approx(0.0)


def test_hole_near_pour_warns(tmp_path):
    # 3 mm hole at x=3.3 -> edge at x=4.8, pour at x=5.0 -> 0.2 mm clearance,
    # below recommended 0.25 mm but above the 0.15 mm hard limit.
    r = _run(tmp_path, _POUR, "board-NPTH.drl", _npth(3.3, 10, 3.0))
    assert r.status == "warning"
    assert r.metric.measured_value == pytest.approx(0.20, abs=0.01)


def test_hole_comfortably_clear_passes(tmp_path):
    # 3 mm hole at x=2.5 -> edge at x=4.0, pour at x=5.0 -> 1.0 mm clearance.
    r = _run(tmp_path, _POUR, "board-NPTH.drl", _npth(2.5, 10, 3.0))
    assert r.status == "pass"


def test_intentional_grounding_ring_not_flagged(tmp_path):
    # Only copper is a small 2 mm pad centred on the hole -> the hole's own ring.
    # Small extent (<= 3 mm) => excluded, and nothing else is near => pass.
    ring = _copper_fill(9.0, 9.0, 11.0, 11.0)
    r = _run(tmp_path, ring, "board-NPTH.drl", _npth(10, 10, 3.0))
    assert r.status == "pass"


def test_plated_only_drill_is_not_applicable(tmp_path):
    # A combined (plated) drill file -> no NPTH layer -> nothing to evaluate.
    r = _run(tmp_path, _POUR, "board.drl", _npth(10, 10, 3.0))
    assert r.status == "not_applicable"
