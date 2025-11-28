# pcb_dfm/geometry/primitives.py

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Iterable


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
    Simple polygon defined by ordered vertices in mm.

    This is intentionally minimal. Later you can attach holes,
    nets, or shape metadata as needed.
    """
    vertices: List[Point2D]

    def bounds(self) -> Bounds:
        return Bounds.from_points(self.vertices)
