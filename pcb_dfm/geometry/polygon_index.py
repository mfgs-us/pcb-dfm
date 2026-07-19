# pcb_dfm/geometry/polygon_index.py

"""
Uniform-grid spatial index over polygons / bounding boxes.

This is a pure-Python (no third-party dependencies) broad-phase index used to
replace O(n^2) all-pairs bounding-box scans in the DFM checks. It buckets each
item's axis-aligned bounding box into every grid cell that box overlaps, so a
spatial query only has to look at a small, local set of candidate items rather
than every other item on the board.

The index is a *broad phase* only: queries return a **superset** of the items
that truly satisfy the spatial predicate. Callers are expected to apply their
own exact geometry test (bbox distance, polygon intersection, containment, ...)
to the returned candidates. This keeps results identical to a brute-force scan
while pruning the far-apart pairs that could never match.

Cell size defaults to the median item "feature size" (the larger of the box
width/height), which keeps the average number of items per cell bounded for
typical board geometry. It can also be supplied explicitly by the caller, which
is what the checks do when they need a specific, reproducible cell size.

Coordinates are in mm, matching :mod:`pcb_dfm.geometry.primitives`.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Sequence, Tuple, Union

from .primitives import Bounds, Polygon

Cell = Tuple[int, int]
# An input item is either a Polygon (id is its position in the list), or an
# explicit (id, Bounds) / (id, Polygon) pair.
ItemInput = Union[Polygon, Tuple[object, "Bounds | Polygon"]]

_DEFAULT_CELL_SIZE = 1.0  # mm, fallback when no usable feature size exists


def _as_bounds(obj) -> Bounds:
    """Return a Bounds for either a Bounds or something with .bounds()."""
    if isinstance(obj, Bounds):
        return obj
    if hasattr(obj, "bounds"):
        return obj.bounds()
    raise TypeError(f"Cannot derive bounds from object of type {type(obj)!r}")


def _bounds_overlap(a: Bounds, b: Bounds) -> bool:
    """True if two axis-aligned boxes overlap (touching edges count)."""
    return not (
        a.max_x < b.min_x
        or a.min_x > b.max_x
        or a.max_y < b.min_y
        or a.min_y > b.max_y
    )


class PolygonIndex:
    """
    Uniform-grid broad-phase spatial index over bounding boxes.

    Construct from a list of polygons, or from explicit ``(id, bounds)`` /
    ``(id, polygon)`` pairs::

        idx = PolygonIndex(list_of_polygons)
        idx = PolygonIndex([(pad_id, bounds), ...])
        idx = PolygonIndex.from_bounds([(0, b0), (1, b1)], cell_size=0.5)

    The identifiers returned by the query methods are the caller-supplied ids
    (or the list position when constructed from a bare list of polygons).
    """

    def __init__(
        self,
        items: Iterable[ItemInput] = (),
        cell_size: float | None = None,
    ) -> None:
        ids: List[object] = []
        bounds_list: List[Bounds] = []

        for pos, item in enumerate(items):
            if isinstance(item, tuple) and len(item) == 2:
                item_id, geom = item
                bounds_list.append(_as_bounds(geom))
                ids.append(item_id)
            else:
                bounds_list.append(_as_bounds(item))
                ids.append(pos)

        self._ids: List[object] = ids
        self._bounds: List[Bounds] = bounds_list

        if cell_size is not None:
            if cell_size <= 0.0:
                raise ValueError("cell_size must be positive")
            self.cell_size: float = float(cell_size)
        else:
            self.cell_size = self._auto_cell_size(bounds_list)

        # Grid: cell -> list of item positions (0..n-1), kept in insertion
        # (id) order so that queries iterate candidates deterministically.
        self._grid: Dict[Cell, List[int]] = {}
        for pos, b in enumerate(bounds_list):
            for cell in self._cells_for_bounds(b):
                self._grid.setdefault(cell, []).append(pos)

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def from_polygons(
        cls, polygons: Sequence[Polygon], cell_size: float | None = None
    ) -> "PolygonIndex":
        """Build an index from a sequence of polygons (id == list position)."""
        return cls(polygons, cell_size=cell_size)

    @classmethod
    def from_bounds(
        cls,
        id_bounds: Iterable[Tuple[object, "Bounds | Polygon"]],
        cell_size: float | None = None,
    ) -> "PolygonIndex":
        """Build an index from ``(id, bounds)`` (or ``(id, polygon)``) pairs."""
        return cls(id_bounds, cell_size=cell_size)

    @staticmethod
    def _auto_cell_size(bounds_list: Sequence[Bounds]) -> float:
        """
        Choose a cell size from the median feature size.

        The "feature size" of a box is the larger of its width and height.
        Using the median keeps a few very large items from blowing up the
        cell size, while keeping the typical item roughly one cell wide.
        """
        sizes = []
        for b in bounds_list:
            feat = max(b.max_x - b.min_x, b.max_y - b.min_y)
            if feat > 0.0:
                sizes.append(feat)
        if not sizes:
            return _DEFAULT_CELL_SIZE
        sizes.sort()
        mid = len(sizes) // 2
        if len(sizes) % 2 == 1:
            median = sizes[mid]
        else:
            median = 0.5 * (sizes[mid - 1] + sizes[mid])
        return median if median > 0.0 else _DEFAULT_CELL_SIZE

    # ------------------------------------------------------------------ #
    # Cell math
    # ------------------------------------------------------------------ #
    def _cell_coord(self, v: float) -> int:
        return int(math.floor(v / self.cell_size))

    def cell_of(self, x: float, y: float) -> Cell:
        """Return the ``(cx, cy)`` grid cell containing point ``(x, y)``."""
        return (self._cell_coord(x), self._cell_coord(y))

    def _cells_for_bounds(self, b: Bounds) -> Iterable[Cell]:
        ix0 = self._cell_coord(b.min_x)
        ix1 = self._cell_coord(b.max_x)
        iy0 = self._cell_coord(b.min_y)
        iy1 = self._cell_coord(b.max_y)
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                yield (ix, iy)

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def items_in_cell_block(self, cx: int, cy: int, ring: int = 1) -> List[object]:
        """
        Return ids of all items touching any cell in the square block
        ``[cx-ring, cx+ring] x [cy-ring, cy+ring]``.

        Iteration is ``dx`` (outer) then ``dy`` (inner), and each id is
        emitted at most once, at its first encounter. This deterministic,
        first-seen ordering lets callers reproduce a hand-rolled grid scan
        exactly.
        """
        if ring < 0:
            raise ValueError("ring must be >= 0")
        out: List[object] = []
        seen: set = set()
        for dx in range(-ring, ring + 1):
            for dy in range(-ring, ring + 1):
                for pos in self._grid.get((cx + dx, cy + dy), ()):
                    if pos in seen:
                        continue
                    seen.add(pos)
                    out.append(self._ids[pos])
        return out

    def query_bbox(self, bounds: Bounds) -> List[object]:
        """
        Return ids of items whose bounding box overlaps ``bounds``.

        This is a broad-phase query: it returns a **superset** of the items
        whose polygons truly overlap. The result is refined to items whose
        *stored bounding box* actually overlaps the query box (touching edges
        count as overlap), removing candidates that merely shared a grid cell.
        """
        out: List[object] = []
        seen: set = set()
        for cell in self._cells_for_bounds(bounds):
            for pos in self._grid.get(cell, ()):
                if pos in seen:
                    continue
                seen.add(pos)
                if _bounds_overlap(self._bounds[pos], bounds):
                    out.append(self._ids[pos])
        return out

    def nearby(self, bounds: Bounds, radius_mm: float) -> List[object]:
        """
        Return ids of items whose bounding box comes within ``radius_mm`` of
        ``bounds`` (i.e. whose box overlaps ``bounds`` inflated by the radius).

        Like :meth:`query_bbox` this is a broad phase: for items separated
        diagonally the true center-to-center gap may exceed ``radius_mm``, so
        callers should still apply an exact distance test to the candidates.
        """
        if radius_mm < 0.0:
            raise ValueError("radius_mm must be >= 0")
        inflated = Bounds(
            bounds.min_x - radius_mm,
            bounds.min_y - radius_mm,
            bounds.max_x + radius_mm,
            bounds.max_y + radius_mm,
        )
        return self.query_bbox(inflated)

    # ------------------------------------------------------------------ #
    # Dunder / introspection
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._ids)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"PolygonIndex(n={len(self._ids)}, cell_size={self.cell_size:g}, "
            f"cells={len(self._grid)})"
        )
