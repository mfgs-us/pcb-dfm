"""Lock-in tests for the ACCURATE-tier geometry promotions.

These exercise the *true polygon* geometry paths that distinguish the promoted
checks from their old bounding-box approximations. Each test is written so it
would FAIL under the previous bbox-only implementation, pinning the promotion:

  * copper_to_edge_distance  -> measures copper against real outline contours,
    including interior cutouts a board-bbox method is blind to.
  * solder_mask_web          -> measures true edge-to-edge web width between
    diagonally offset openings a bbox gap over-estimates.
  * fillet_radius_milling    -> analyses every closed contour (perimeter + each
    internal cutout), not just a single all-edges-in-one-loop outline.
"""

from __future__ import annotations

import math

from pcb_dfm.geometry.primitives import Point2D, Polygon


def _rect(x0, y0, x1, y1) -> Polygon:
    return Polygon(vertices=[
        Point2D(x0, y0), Point2D(x1, y0), Point2D(x1, y1), Point2D(x0, y1),
    ])


# --- copper_to_edge_distance / solder_mask_web share this primitive ----------

def test_min_distance_between_polygons_catches_interior_cutout():
    """Copper 0.10 mm from the wall of an interior cutout must measure 0.10 mm.

    A board-bbox method sees only the outer rectangle and would report the
    (much larger) distance to the perimeter, silently passing this violation.
    """
    from pcb_dfm.checks.impl_copper_to_edge_distance import _min_distance_between_polygons

    cutout = _rect(5.0, 5.0, 9.0, 9.0)          # interior slot wall at x=5
    copper = _rect(4.4, 6.0, 4.9, 8.0)          # copper right edge at x=4.9
    d = _min_distance_between_polygons(copper, cutout)
    assert math.isclose(d, 0.10, abs_tol=1e-6), d


def test_solder_mask_web_true_edge_distance_beats_bbox_gap():
    """A diagonally offset *rotated* opening (diamond): its bounding box is far
    larger than the shape, so the bbox gap under-reads the real web while the
    true edge distance measures it correctly.

    For axis-aligned rectangles bbox gap == true distance, so a rotated opening
    is required to exercise (and pin) the promotion."""
    from pcb_dfm.checks.impl_solder_mask_web import (
        _bbox_distance_mm,
        _min_distance_between_polygons,
    )

    square = _rect(0.0, 0.0, 1.0, 1.0)
    # Diamond (rotated square) centred at (2, 2), "radius" 0.5.
    diamond = Polygon(vertices=[
        Point2D(2.5, 2.0), Point2D(2.0, 2.5), Point2D(1.5, 2.0), Point2D(2.0, 1.5),
    ])

    class _B:
        def __init__(self, p):
            bb = p.bounds()
            self.min_x, self.max_x = bb.min_x, bb.max_x
            self.min_y, self.max_y = bb.min_y, bb.max_y

    true_d = _min_distance_between_polygons(square, diamond)
    bbox_d = _bbox_distance_mm(_B(square), _B(diamond))
    # True web: square corner (1,1) to the diamond's lower-left face (x+y=3.5),
    # perpendicular distance 1.5/sqrt(2).
    assert math.isclose(true_d, 1.5 / math.sqrt(2.0), abs_tol=1e-6), true_d
    # The bbox gap strictly under-reads that real web — exactly the false-narrow
    # web the promotion removes.
    assert bbox_d < true_d


# --- fillet_radius_milling multi-contour support -----------------------------

def _edges_of_rect(x0, y0, x1, y1):
    """Return 4 line edges (p_start, p_end, kind, radius, direction) CCW."""
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    edges = []
    for i in range(4):
        p, q = pts[i], pts[(i + 1) % 4]
        edges.append((p, q, "line", None, None))
    return edges


def test_build_loops_returns_a_loop_per_contour():
    """Perimeter + an interior cutout = two closed contours. The old single-loop
    builder returned None here (not all edges chain into ONE loop) -> the check
    was not_applicable; now each contour is analysed."""
    from pcb_dfm.checks.impl_fillet_radius_milling import _build_loops

    edges = _edges_of_rect(0, 0, 20, 14) + _edges_of_rect(6, 5, 10, 9)
    loops = _build_loops(edges)
    assert loops is not None
    assert len(loops) == 2
    assert all(len(v) == 4 for v, _ in loops)


def test_internal_cutout_sharp_corner_is_detected():
    """A rectangular interior cutout has four sharp (radius 0) internal corners
    a router bit cannot cut. They must surface as concave-corner candidates."""
    from pcb_dfm.checks.impl_fillet_radius_milling import (
        _build_loops,
        _concave_corner_radii,
        _hole_flags,
    )

    edges = _edges_of_rect(0, 0, 20, 14) + _edges_of_rect(6, 5, 10, 9)
    loops = _build_loops(edges)
    flags = _hole_flags(loops)
    # Exactly one contour (the interior cutout) is a hole; the perimeter is not.
    assert flags.count(True) == 1
    radii = []
    for (vertices, edge_infos), is_hole in zip(loops, flags):
        radii.extend(_concave_corner_radii(vertices, edge_infos, is_hole))
    # The interior cutout contributes four 0-radius (sharp) internal corners;
    # the convex outer perimeter contributes none.
    sharp = [r for r in radii if r[0] <= 1e-9]
    assert len(sharp) == 4
