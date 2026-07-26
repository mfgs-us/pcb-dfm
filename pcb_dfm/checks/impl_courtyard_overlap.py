"""Design advisory: component courtyards on the same side must not overlap.

A footprint's courtyard is the keep-out its designer reserved for placement and
rework access; two overlapping courtyards on the same side mean the parts are too
close to place, inspect, or rework -- a design-for-assembly collision. This reads
the real courtyard geometry, so it is stricter and more correct than the
pad-distance ``component_to_component_spacing`` heuristic.

Needs courtyard geometry in the design data (e.g. a KiCad board's ``*.CrtYd``
layers); otherwise not_applicable. Opposite-side parts may share XY (they stack
top/bottom) and are not compared.
"""

from __future__ import annotations

from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, is_assembly_body, na

_EPS_MM = 0.01  # ignore edge-touch / float noise; only real overlap counts

Poly = List[Tuple[float, float]]


def _bbox(p: Poly) -> Tuple[float, float, float, float]:
    xs = [x for (x, _) in p]
    ys = [y for (_, y) in p]
    return min(xs), min(ys), max(xs), max(ys)


def _overlap(a: Poly, b: Poly) -> bool:
    """True when two convex polygons overlap by more than _EPS_MM (separating-axis
    theorem). Edge contact / float noise does not count."""
    if len(a) < 2 or len(b) < 2:
        return False
    # Cheap bbox reject first.
    ax0, ay0, ax1, ay1 = _bbox(a)
    bx0, by0, bx1, by1 = _bbox(b)
    if ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0:
        return False
    for poly in (a, b):
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            nx, ny = -(y2 - y1), (x2 - x1)          # edge normal
            length = (nx * nx + ny * ny) ** 0.5
            if length == 0:
                continue
            nx, ny = nx / length, ny / length        # normalize -> mm projections
            a_proj = [px * nx + py * ny for (px, py) in a]
            b_proj = [px * nx + py * ny for (px, py) in b]
            # Separated (or merely touching) on this axis -> not a real overlap.
            if min(max(a_proj), max(b_proj)) - max(min(a_proj), min(b_proj)) <= _EPS_MM:
                return False
    return True


@register_check("courtyard_overlap")
def run_courtyard_overlap(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    comps = [c for c in (getattr(dd, "components", None) or [])
             if getattr(c, "placed", True) and getattr(c, "courtyard", None) is not None
             and is_assembly_body(c)]
    if len(comps) < 2:
        return na(ctx, "Needs at least two placed component bodies with courtyard "
                       "geometry (e.g. a KiCad board's *.CrtYd layers).")

    # Group by side (unknown side -> treated as top, matching placement default).
    def side(c) -> str:
        return (getattr(c, "side", None) or "top").lower()

    pairs: List[Tuple[str, str, Poly]] = []
    by_side: dict = {}
    for c in comps:
        by_side.setdefault(side(c), []).append(c)
    for group in by_side.values():
        n = len(group)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = group[i], group[j]
                if _overlap(a.courtyard, b.courtyard):
                    pairs.append((a.ref, b.ref, a.courtyard))

    count = len(pairs)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        "No same-side component courtyards overlap.")
    ra, rb, poly = pairs[0]
    xs = [x for (x, _) in poly]
    ys = [y for (_, y) in poly]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    return advisory(
        ctx, True, count_metric(count),
        f"{count} same-side component courtyard pair(s) overlap (e.g. {ra} / {rb}) "
        f"-- too close to place, inspect, or rework. Increase the spacing.",
        ViolationLocation(layer="placement", x_mm=cx, y_mm=cy,
                          notes=f"Courtyards of {ra} and {rb} overlap."),
    )
