"""Clear-polarity (negative) regions must read as copper voids, not extra copper.

A plane antipad -- and any Gerber clear-polarity (LPC) cut-out -- removes the
copper beneath it. The renderer used to ignore polarity and add the antipad as
*positive* copper, so a via in it read as buried-in-copper (0 mm clearance) and a
netlist point in it leaked into the plane's net. These pin the fix: clear regions
become HOLES, and the copper-coverage tests are hole-aware.
"""

from __future__ import annotations

import zipfile

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check
from pcb_dfm.geometry.gerber_backend import gerber_polygons_mm
from pcb_dfm.geometry.primitives import Point2D, Polygon

_HDR = "%FSLAX46Y46*%\n%MOMM*%\n"


def test_polygon_contains_point_is_hole_aware():
    ext = [Point2D(0, 0), Point2D(10, 0), Point2D(10, 10), Point2D(0, 10)]
    hole = [Point2D(4, 4), Point2D(6, 4), Point2D(6, 6), Point2D(4, 6)]
    p = Polygon(vertices=ext, holes=[hole])
    assert p.contains_point(1, 1) is True        # copper
    assert p.contains_point(5, 5) is False       # in the hole -> void
    assert p.contains_point(20, 20) is False      # outside entirely


def _disc_with_clear_antipad() -> str:
    # 5 mm solid disc (dark) with a 1 mm clear antipad at its centre.
    return (_HDR + "%ADD10C,5.000000*%\n%ADD11C,1.000000*%\n"
            "%LPD*%\nD10*\nX5000000Y5000000D03*\n"
            "%LPC*%\nD11*\nX5000000Y5000000D03*\nM02*\n")


def test_clear_polarity_becomes_a_hole(tmp_path):
    p = tmp_path / "disc.gbr"
    p.write_text(_disc_with_clear_antipad())
    polys = gerber_polygons_mm(p)
    assert len(polys) == 1 and len(polys[0].holes) == 1
    assert polys[0].contains_point(5.0, 5.0) is False   # antipad is a void
    assert polys[0].contains_point(7.0, 5.0) is True     # copper ring


def _plane_board(tmp_path):
    """A 20 mm plane with a 2 mm clear antipad at (5,5) and a 0.3 mm via there."""
    plane = (_HDR + "%ADD10C,0.010000*%\n%ADD11C,2.000000*%\n"
             "%LPD*%\nG36*\nX0Y0D02*\nX20000000Y0D01*\nX20000000Y20000000D01*\n"
             "X0Y20000000D01*\nX0Y0D01*\nG37*\n"
             "%LPC*%\nD11*\nX5000000Y5000000D03*\nM02*\n")
    outline = (_HDR + "%ADD10C,0.010000*%\nD10*\nX0Y0D02*\nX20000000Y0D01*\n"
               "X20000000Y20000000D01*\nX0Y20000000D01*\nX0Y0D01*\nM02*\n")
    drill = "M48\nMETRIC,TZ\nT1C0.300\n%\nT1\nX5.000Y5.000\nT0\nM30\n"
    z = tmp_path / "plane.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("plane.gtl", plane)
        zf.writestr("plane.gko", outline)
        zf.writestr("plane.drl", drill)
    return z


def test_via_in_antipad_is_not_zero_clearance(tmp_path):
    # Antipad radius 1.0 mm, via radius 0.15 mm -> real clearance ~0.85 mm.
    # Before the fix the antipad was ignored -> via buried in the plane -> 0 mm.
    z = _plane_board(tmp_path)
    r = run_single_check(z, load_check_definition("via_to_copper_clearance"))
    assert r.metric.measured_value is not None
    assert r.metric.measured_value > 0.5   # not the false 0.0 mm
    assert abs(r.metric.measured_value - 0.85) < 0.05
