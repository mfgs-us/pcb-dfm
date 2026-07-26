"""Correctness tests for the design_advisory checks.

Each builds a tiny board that trips exactly the behavior under test, and asserts
the advisory outcome. Design-advisory checks never hard-fail: a flagged board is
a `warning`, a clean one is `pass`, and an inapplicable one is `not_applicable`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check

_HDR = "%FSLAX46Y46*%\n%MOMM*%\n"


def _zip(tmp_path: Path, files: dict) -> Path:
    p = tmp_path / "b.zip"
    with zipfile.ZipFile(p, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    return p


def _seg(x1, y1, x2, y2):
    return (f"X{int(round(x1 * 1e6))}Y{int(round(y1 * 1e6))}D02*\n"
            f"X{int(round(x2 * 1e6))}Y{int(round(y2 * 1e6))}D01*\n")


def _outline(pts) -> str:
    """Closed outline through pts (list of (x,y)), thin aperture."""
    s = _HDR + "%ADD10C,0.010000*%\nD10*\n"
    s += f"X{int(pts[0][0] * 1e6)}Y{int(pts[0][1] * 1e6)}D02*\n"
    for (x, y) in pts[1:]:
        s += f"X{int(round(x * 1e6))}Y{int(round(y * 1e6))}D01*\n"
    s += f"X{int(pts[0][0] * 1e6)}Y{int(pts[0][1] * 1e6)}D01*\nM02*\n"
    return s


def _rect_outline(w, h):
    return _outline([(0, 0), (w, 0), (w, h), (0, h)])


def _pad(cx, cy, w, h, ap="11"):
    return (_HDR + f"%ADD{ap}R,{w:.6f}X{h:.6f}*%\nD{ap}*\n"
            f"X{int(round(cx * 1e6))}Y{int(round(cy * 1e6))}D03*\nM02*\n")


def _circle(cx, cy, dia, ap="12"):
    return (_HDR + f"%ADD{ap}C,{dia:.6f}*%\nD{ap}*\n"
            f"X{int(round(cx * 1e6))}Y{int(round(cy * 1e6))}D03*\nM02*\n")


def _run(z, cid, **kw):
    return run_single_check(z, load_check_definition(cid), **kw)


# ---- outline_sharp_corners ------------------------------------------------
def test_sharp_corners_rectangle_passes(tmp_path):
    z = _zip(tmp_path, {"board.gko": _rect_outline(10, 10)})
    assert _run(z, "outline_sharp_corners").status == "pass"


def test_sharp_corners_spike_warns(tmp_path):
    # A rectangle with a narrow triangular spike on the right side (acute point).
    pts = [(0, 0), (10, 0), (10, 4), (16, 5), (10, 6), (10, 10), (0, 10)]
    z = _zip(tmp_path, {"board.gko": _outline(pts)})
    r = _run(z, "outline_sharp_corners")
    assert r.status == "warning"
    assert r.metric.measured_value >= 1


# ---- silkscreen_off_board -------------------------------------------------
def test_silk_off_board_clipped_warns(tmp_path):
    # Silk line running from inside the board out past the right edge.
    silk = _HDR + "%ADD20C,0.150000*%\nD20*\n" + _seg(8.0, 5.0, 12.0, 5.0) + "M02*\n"
    z = _zip(tmp_path, {"board.gko": _rect_outline(10, 10), "board.gto": silk})
    assert _run(z, "silkscreen_off_board").status == "warning"


def test_silk_inside_passes(tmp_path):
    silk = _HDR + "%ADD20C,0.150000*%\nD20*\n" + _seg(2.0, 5.0, 6.0, 5.0) + "M02*\n"
    z = _zip(tmp_path, {"board.gko": _rect_outline(10, 10), "board.gto": silk})
    assert _run(z, "silkscreen_off_board").status == "pass"


# ---- component_edge_clearance --------------------------------------------
def test_component_edge_clearance_warns(tmp_path):
    # 1 mm pad centred 0.6 mm from the left edge -> pad edge ~0.1 mm from board.
    z = _zip(tmp_path, {"board.gko": _rect_outline(10, 10), "board.gtl": _pad(0.6, 5, 1.0, 1.0)})
    r = _run(z, "component_edge_clearance")
    assert r.status == "warning"
    assert r.metric.measured_value < 0.5


def test_component_edge_clearance_passes(tmp_path):
    z = _zip(tmp_path, {"board.gko": _rect_outline(10, 10), "board.gtl": _pad(5, 5, 1.0, 1.0)})
    assert _run(z, "component_edge_clearance").status == "pass"


# ---- floating_copper ------------------------------------------------------
def test_floating_copper_isolated_warns(tmp_path):
    # A single isolated 2x2 mm copper flash, no drill, nothing else touching it.
    z = _zip(tmp_path, {"board.gko": _rect_outline(20, 20), "board.gtl": _pad(10, 10, 2.0, 2.0)})
    r = _run(z, "floating_copper")
    assert r.status == "warning"
    assert r.metric.measured_value >= 1


# ---- fiducial_coverage ----------------------------------------------------
def test_fiducial_coverage_smt_without_fiducials_warns(tmp_path):
    # SMT board (has a paste layer) but no fiducial-like features.
    files = {
        "board.gko": _rect_outline(20, 20),
        "board.gtl": _pad(10, 10, 0.5, 0.5),
        "board.gtp": _pad(10, 10, 0.4, 0.4),  # paste -> SMT
    }
    r = _run(_zip(tmp_path, files), "fiducial_coverage")
    assert r.status == "warning"


def test_fiducial_coverage_no_paste_not_applicable(tmp_path):
    files = {"board.gko": _rect_outline(20, 20), "board.gtl": _pad(10, 10, 0.5, 0.5)}
    assert _run(_zip(tmp_path, files), "fiducial_coverage").status == "not_applicable"


# ---- teardrop_presence ----------------------------------------------------
def test_teardrop_thin_annular_via_warns(tmp_path):
    # 0.3 mm via in a 0.35 mm round pad -> annular ~0.025 mm (< 0.1 mm floor).
    drill = "M48\nMETRIC,TZ\nT1C0.300\n%\nT1\nX5.0Y5.0\nT0\nM30\n"
    z = _zip(tmp_path, {"board.gtl": _circle(5.0, 5.0, 0.35), "board.drl": drill})
    r = _run(z, "teardrop_presence")
    assert r.status == "warning"
    assert r.metric.measured_value >= 1
