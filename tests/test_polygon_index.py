# tests/test_polygon_index.py

"""
Unit tests for the uniform-grid spatial index (pcb_dfm.geometry.polygon_index).

All test data is built from fixed arithmetic patterns (no `random`) so runs are
fully deterministic.
"""

from __future__ import annotations

from pcb_dfm.geometry.polygon_index import PolygonIndex
from pcb_dfm.geometry.primitives import Bounds, Point2D, Polygon


def _box(min_x, min_y, max_x, max_y) -> Bounds:
    return Bounds(min_x, min_y, max_x, max_y)


def _bounds_overlap(a: Bounds, b: Bounds) -> bool:
    return not (
        a.max_x < b.min_x or a.min_x > b.max_x or a.max_y < b.min_y or a.min_y > b.max_y
    )


def _rect_polygon(min_x, min_y, max_x, max_y) -> Polygon:
    return Polygon(
        [
            Point2D(min_x, min_y),
            Point2D(max_x, min_y),
            Point2D(max_x, max_y),
            Point2D(min_x, max_y),
        ]
    )


# --------------------------------------------------------------------------- #
# Basic behaviour
# --------------------------------------------------------------------------- #
def test_overlapping_bbox_returns_candidate():
    idx = PolygonIndex.from_bounds(
        [(0, _box(0.0, 0.0, 1.0, 1.0)), (1, _box(0.5, 0.5, 1.5, 1.5))],
        cell_size=1.0,
    )
    # A query box overlapping both should return both.
    got = set(idx.query_bbox(_box(0.4, 0.4, 0.6, 0.6)))
    assert got == {0, 1}


def test_disjoint_far_apart_returns_none():
    idx = PolygonIndex.from_bounds(
        [(0, _box(0.0, 0.0, 1.0, 1.0)), (1, _box(100.0, 100.0, 101.0, 101.0))],
        cell_size=1.0,
    )
    # Query near item 0 must not surface the far-away item 1.
    got = set(idx.query_bbox(_box(0.2, 0.2, 0.8, 0.8)))
    assert got == {0}

    # Query in empty space returns nothing.
    assert idx.query_bbox(_box(50.0, 50.0, 51.0, 51.0)) == []


def test_touching_edges_count_as_overlap():
    idx = PolygonIndex.from_bounds(
        [(0, _box(0.0, 0.0, 1.0, 1.0))], cell_size=1.0
    )
    # Shares only the x == 1.0 edge.
    assert set(idx.query_bbox(_box(1.0, 0.0, 2.0, 1.0))) == {0}


def test_from_polygons_uses_list_position_as_id():
    polys = [_rect_polygon(0, 0, 1, 1), _rect_polygon(10, 10, 11, 11)]
    idx = PolygonIndex.from_polygons(polys, cell_size=2.0)
    assert set(idx.query_bbox(_box(0.5, 0.5, 0.6, 0.6))) == {0}
    assert set(idx.query_bbox(_box(10.5, 10.5, 10.6, 10.6))) == {1}


def test_auto_cell_size_is_positive():
    polys = [_rect_polygon(0, 0, 2, 2), _rect_polygon(5, 5, 8, 9)]
    idx = PolygonIndex.from_polygons(polys)
    assert idx.cell_size > 0.0


def test_len_reports_item_count():
    idx = PolygonIndex.from_bounds(
        [(i, _box(i, 0, i + 0.5, 0.5)) for i in range(7)], cell_size=1.0
    )
    assert len(idx) == 7


def test_nearby_inflates_query():
    idx = PolygonIndex.from_bounds(
        [(0, _box(0.0, 0.0, 1.0, 1.0)), (1, _box(3.0, 0.0, 4.0, 1.0))],
        cell_size=1.0,
    )
    # Gap between the two boxes is 2.0 mm.
    assert set(idx.nearby(_box(0.0, 0.0, 1.0, 1.0), radius_mm=1.0)) == {0}
    assert set(idx.nearby(_box(0.0, 0.0, 1.0, 1.0), radius_mm=2.5)) == {0, 1}


def test_items_in_cell_block_first_seen_order_and_dedup():
    # A wide box spans several cells; it must appear exactly once.
    idx = PolygonIndex.from_bounds(
        [
            (0, _box(0.0, 0.0, 0.5, 0.5)),   # cell (0,0)
            (1, _box(0.0, 0.0, 5.0, 0.5)),   # spans cells (0..5, 0)
            (2, _box(2.0, 2.0, 2.5, 2.5)),   # cell (2,2)
        ],
        cell_size=1.0,
    )
    block = idx.items_in_cell_block(0, 0, ring=1)
    assert block.count(1) == 1  # deduplicated despite spanning many cells
    assert set(block) == {0, 1}  # item 2 (cell 2,2) is outside a ring-1 block
    # Item 0 and 1 both live in cell (0,0); 0 was inserted first.
    assert block.index(0) < block.index(1)


# --------------------------------------------------------------------------- #
# Superset property vs brute-force O(n^2) overlap check
# --------------------------------------------------------------------------- #
def _deterministic_boxes(n: int):
    """
    Build n boxes from a fixed arithmetic pattern (no randomness).

    Positions and sizes are derived from modular arithmetic on the index so the
    layout is spread out, varied, and reproducible across runs/machines.
    """
    boxes = []
    for i in range(n):
        # Spread across a coarse grid with an offset that walks around.
        x = (i * 7) % 53 + ((i * 3) % 5) * 0.25
        y = (i * 11) % 47 + ((i * 5) % 4) * 0.25
        w = 0.5 + ((i * 13) % 6) * 0.5   # 0.5 .. 3.0
        h = 0.5 + ((i * 17) % 4) * 0.5   # 0.5 .. 2.0
        boxes.append(_box(x, y, x + w, y + h))
    return boxes


def test_query_bbox_is_superset_of_bruteforce():
    boxes = _deterministic_boxes(200)
    id_bounds = list(enumerate(boxes))

    for cell_size in (0.5, 1.0, 2.0, 3.7):
        idx = PolygonIndex.from_bounds(id_bounds, cell_size=cell_size)
        for i, qi in enumerate(boxes):
            # Brute-force O(n^2): every box that truly overlaps box i.
            brute = {j for j, qj in enumerate(boxes) if _bounds_overlap(qi, qj)}
            candidates = set(idx.query_bbox(qi))
            # The index must never miss a true overlap.
            assert brute <= candidates, (
                f"cell_size={cell_size} i={i} missed={brute - candidates}"
            )
            # And query_bbox refines to real bbox overlaps only.
            assert candidates == brute


def test_auto_cell_size_query_is_superset():
    boxes = _deterministic_boxes(120)
    idx = PolygonIndex.from_polygons(
        [_rect_polygon(b.min_x, b.min_y, b.max_x, b.max_y) for b in boxes]
    )
    for i, qi in enumerate(boxes):
        brute = {j for j, qj in enumerate(boxes) if _bounds_overlap(qi, qj)}
        candidates = set(idx.query_bbox(qi))
        assert brute <= candidates
