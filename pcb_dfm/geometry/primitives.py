# pcb_dfm/geometry/primitives.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List


@dataclass(frozen=True)
class Point2D:
    x: float
    y: float


@dataclass
class Bounds:
    """
    Axis aligned bounding box in mm.
    """
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def expand_to_include(self, pt: Point2D) -> None:
        self.min_x = min(self.min_x, pt.x)
        self.min_y = min(self.min_y, pt.y)
        self.max_x = max(self.max_x, pt.x)
        self.max_y = max(self.max_y, pt.y)

    def include_bounds(self, other: "Bounds") -> None:
        self.min_x = min(self.min_x, other.min_x)
        self.min_y = min(self.min_y, other.min_y)
        self.max_x = max(self.max_x, other.max_x)
        self.max_y = max(self.max_y, other.max_y)

    @classmethod
    def from_points(cls, points: Iterable[Point2D]) -> "Bounds":
        pts = list(points)
        if not pts:
            raise ValueError("Cannot compute bounds from empty point list")
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        return cls(min(xs), min(ys), max(xs), max(ys))


@dataclass
class Polygon:
    """
    Filled polygon in mm: an exterior ``vertices`` ring, plus zero or more
    interior ``holes`` (clearance voids -- e.g. a plane antipad, or any region
    a Gerber clear-polarity object cut out of the copper).

    ``bounds`` is the exterior extent; the holes are always inside it.
    """
    vertices: List[Point2D]
    holes: List[List[Point2D]] = field(default_factory=list)

    def bounds(self) -> Bounds:
        return Bounds.from_points(self.vertices)

    def contains_point(self, x: float, y: float) -> bool:
        """True when (x, y) is on copper: inside the exterior ring and outside
        every hole. Holes make a plane antipad / cleared region read as the
        void it physically is, not as copper."""
        if not _ring_contains(x, y, self.vertices):
            return False
        return not any(_ring_contains(x, y, h) for h in self.holes)


def _ring_contains(x: float, y: float, ring: List[Point2D]) -> bool:
    """Even-odd ray cast: is (x, y) inside the closed polygon ``ring``?"""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i].x, ring[i].y
        xj, yj = ring[j].x, ring[j].y
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside
