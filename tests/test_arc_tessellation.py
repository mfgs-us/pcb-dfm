"""Pin the arc-tessellation handedness convention (#14).

gerbonara reports the two kinds of outline arc in *different* frames:
aperture-derived arcs (a flash's shape, a stroked line's round end caps) come
back in an inverted frame, while path-derived arcs (a G36 region boundary drawn
with G02/G03) are already in the Gerber y-up frame. Sweeping either one the
wrong way is silent -- you get a plausible polygon of entirely the wrong shape.

These tests compare against shapes whose area and bounding box are known
analytically, because that is the only way to catch a wrong sweep: the earlier
bug produced round end caps that folded *into* the stroke, turning every
stroked trace into a self-intersecting bowtie whose boundary ran along the
segment's own axis. That read as ~0 mm clearance board-wide -- an annular ring
of exactly 0.000 mm on generous 60 mil pads.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.geometry.gerber_backend import _object_polygons_mm  # noqa: E402

# 24 segments per full circle, inscribed, so a tessellated disc loses ~1% of its
# area. Tolerances below are set just wide enough for that.
_AREA_TOL = 0.02  # relative
_BBOX_TOL = 1e-6  # absolute mm; cardinal points land exactly on vertices

_HEADER = "%FSLAX26Y26*%\n%MOMM*%\n"


def _emit(tmp_path: Path, body: str, name: str = "t.gbr") -> Path:
    p = tmp_path / name
    p.write_text(_HEADER + body)
    return p


def _polys(path: Path):
    from gerbonara import GerberFile

    out = []
    for obj in GerberFile.open(str(path)).objects:
        out.extend(_object_polygons_mm(obj))
    return out


def _area(poly) -> float:
    v = [(p.x, p.y) for p in poly.vertices]
    s = 0.0
    for i in range(len(v)):
        x1, y1 = v[i]
        x2, y2 = v[(i + 1) % len(v)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _bbox(poly):
    b = poly.bounds()
    return (b.max_x - b.min_x, b.max_y - b.min_y)


def _largest(polys):
    return max(polys, key=_area)


# --------------------------------------------------------------------------
# Aperture-derived arcs (flashes) -- the inverted frame
# --------------------------------------------------------------------------
def test_circle_flash_area_and_bbox(tmp_path):
    p = _emit(tmp_path, "%ADD10C,1.0*%\nD10*\nX0Y0D03*\nM02*\n")
    poly = _largest(_polys(p))
    assert _area(poly) == pytest.approx(math.pi * 0.5**2, rel=_AREA_TOL)
    w, h = _bbox(poly)
    assert w == pytest.approx(1.0, abs=_BBOX_TOL)
    assert h == pytest.approx(1.0, abs=_BBOX_TOL)


def test_obround_flash_area_and_bbox(tmp_path):
    # A 3.0 x 1.0 obround: a 2.0 x 1.0 rectangle plus two semicircular caps.
    p = _emit(tmp_path, "%ADD11O,3.0X1.0*%\nD11*\nX0Y0D03*\nM02*\n")
    poly = _largest(_polys(p))
    expected = 2.0 * 1.0 + math.pi * 0.5**2
    assert _area(poly) == pytest.approx(expected, rel=_AREA_TOL)
    w, h = _bbox(poly)
    assert w == pytest.approx(3.0, abs=_BBOX_TOL)
    assert h == pytest.approx(1.0, abs=_BBOX_TOL)


def test_stroked_line_is_a_stadium_not_a_bowtie(tmp_path):
    """The #14 regression, in its simplest form.

    A 2 mm line stroked with a 1 mm round aperture is a stadium: its caps
    bulge 0.5 mm *beyond* each endpoint, giving a 3.0 x 1.0 mm extent. With the
    caps swept the wrong way they fold inward, the bbox collapses to the bare
    2.0 mm segment length, and the outline passes through the segment axis --
    which is what made pads report zero clearance to their own drill.
    """
    p = _emit(tmp_path, "%ADD10C,1.0*%\nD10*\nX0Y0D02*\nX2000000Y0D01*\nM02*\n")
    poly = _largest(_polys(p))

    w, h = _bbox(poly)
    assert w == pytest.approx(3.0, abs=_BBOX_TOL), "caps must extend beyond the endpoints"
    assert h == pytest.approx(1.0, abs=_BBOX_TOL)

    expected = 2.0 * 1.0 + math.pi * 0.5**2
    assert _area(poly) == pytest.approx(expected, rel=_AREA_TOL)

    # The stroke's own axis must be strictly interior: no boundary vertex may
    # land on the segment's midpoint, which is exactly what the folded cap did.
    for v in poly.vertices:
        assert math.hypot(v.x - 1.0, v.y - 0.0) > 0.4


# --------------------------------------------------------------------------
# Path-derived arcs (regions) -- the un-inverted frame
# --------------------------------------------------------------------------
def test_region_g03_quarter_arc_sweeps_the_short_way(tmp_path):
    """A quarter-pie region: (0,0) -> (1,0) -> CCW arc to (0,1) -> close.

    Area is pi/4 within a unit bbox. Swept the wrong way the same statement
    becomes a 270-degree sweep -- roughly three times the area, in a 2x2 bbox --
    so this pins the region convention as distinct from the aperture one.
    """
    p = _emit(
        tmp_path,
        "%ADD10C,0.01*%\nG75*\nD10*\nG36*\n"
        "X0Y0D02*\nX1000000Y0D01*\nG03*\nX0Y1000000I-1000000J0D01*\n"
        "G01*\nX0Y0D01*\nG37*\nM02*\n",
    )
    poly = _largest(_polys(p))
    assert _area(poly) == pytest.approx(math.pi / 4.0, rel=_AREA_TOL)
    w, h = _bbox(poly)
    assert w == pytest.approx(1.0, abs=1e-4)
    assert h == pytest.approx(1.0, abs=1e-4)


def test_region_and_flash_conventions_do_not_collapse(tmp_path):
    """Guard against 'fixing' this by flipping both the same way.

    Applying one convention to both is self-consistent but wrong for one of
    them, so assert the two shapes are simultaneously correct in one file.
    """
    p = _emit(
        tmp_path,
        "%ADD10C,1.0*%\n%ADD11C,0.01*%\n"
        "D10*\nX0Y0D03*\n"
        "G75*\nD11*\nG36*\n"
        "X5000000Y0D02*\nX6000000Y0D01*\nG03*\nX5000000Y1000000I-1000000J0D01*\n"
        "G01*\nX5000000Y0D01*\nG37*\nM02*\n",
    )
    polys = _polys(p)
    areas = sorted(_area(x) for x in polys if _area(x) > 1e-6)
    # circle d=1 -> pi/4 ; quarter pie r=1 -> pi/4. Both land on the same value,
    # so compare each against the analytic figure rather than to each other.
    assert len(areas) >= 2
    for a in areas[:2]:
        assert a == pytest.approx(math.pi / 4.0, rel=_AREA_TOL)
