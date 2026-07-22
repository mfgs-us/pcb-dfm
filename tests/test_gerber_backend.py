"""gerbonara parse backend (#3): mm-native polygons, true arcs."""

from __future__ import annotations

import math

from pcb_dfm.geometry.gerber_backend import (
    GERBONARA_AVAILABLE,
    _tessellate_arc,
    gerber_polygons_mm,
)

# A CCW semicircular arc trace from (0,0) to (2,0) about centre (1,0), stroked
# with a 0.2 mm round aperture. Format: 2 integer / 4 decimal digits, mm.
_ARC_GERBER = """%FSLAX24Y24*%
%MOMM*%
%ADD10C,0.20000*%
D10*
G75*
X0Y0D02*
G03X20000Y0I10000J0D01*
M02*
"""


def test_tessellate_arc_follows_the_curve():
    # Half circle radius 1 about origin, from (1,0) to (-1,0) counter-clockwise:
    # the midpoint must bulge to y ~ +1, not sit on the chord (y = 0).
    pts = _tessellate_arc((1.0, 0.0), (-1.0, 0.0), (0.0, 0.0), clockwise=False)
    assert len(pts) >= 8
    assert max(p[1] for p in pts) > 0.9
    # every point lies on the circle
    for x, y in pts:
        assert abs(math.hypot(x, y) - 1.0) < 1e-9


def test_arc_gerber_parses_as_true_curve(tmp_path):
    if not GERBONARA_AVAILABLE:  # pragma: no cover
        return
    f = tmp_path / "arc.gtl"
    f.write_text(_ARC_GERBER, encoding="utf-8")
    polys = gerber_polygons_mm(f)
    assert polys, "arc trace produced no geometry"

    ys = [v.y for p in polys for v in p.vertices]
    xs = [v.x for p in polys for v in p.vertices]
    # A chord approximation would keep the stroke flat around y in [-0.1, 0.1];
    # the true arc bulges to ~1 mm (radius) plus half the 0.2 mm stroke.
    assert max(ys) > 0.8, f"arc was flattened to a chord (max y={max(ys):.3f})"
    # and it spans the full 0..2 mm chord in x
    assert min(xs) < 0.1 and max(xs) > 1.9


def test_polygons_are_mm_not_inches(tmp_path):
    # The file is mm-native; coordinates must come back in mm (not re-scaled).
    if not GERBONARA_AVAILABLE:  # pragma: no cover
        return
    f = tmp_path / "arc.gtl"
    f.write_text(_ARC_GERBER, encoding="utf-8")
    xs = [v.x for p in gerber_polygons_mm(f) for v in p.vertices]
    # 0..2 mm chord, widened by the round cap of the 0.2 mm aperture (±0.1 mm).
    # If units were mis-scaled (inch<->mm) these would be off by 25.4x.
    assert -0.15 < min(xs) < 0.05
    assert 1.95 < max(xs) < 2.25
