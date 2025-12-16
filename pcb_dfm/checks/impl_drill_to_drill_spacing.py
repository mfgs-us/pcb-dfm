from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil, floor, hypot, inf, sqrt
import math
from typing import Dict, List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation

try:
    from gerber import excellon  # type: ignore
except Exception:
    excellon = None  # type: ignore

_INCH_TO_MM = 25.4


@dataclass
class DrillHole:
    x_mm: float
    y_mm: float
    diameter_mm: float


def _cell_key(x: float, y: float, cell: float) -> Tuple[int, int]:
    return (int(floor(x / cell)), int(floor(y / cell)))


def _min_possible_center_dist_between_cells(dx_cells: int, dy_cells: int, cell: float) -> float:
    """
    Lower bound on distance between any two points located in two grid cells offset by (dx_cells, dy_cells).
    """
    dx = max(0, abs(dx_cells) - 1) * cell
    dy = max(0, abs(dy_cells) - 1) * cell
    return hypot(dx, dy)


def _collect_drills(ctx: CheckContext) -> List[DrillHole]:
    if excellon is None:
        return []
    drills: List[DrillHole] = []

    for f in ctx.ingest.files:
        if f.layer_type != "drill":
            continue
        if f.format != "excellon":
            continue

        try:
            drill_file = excellon.read(str(f.path))
        except Exception:
            continue

        try:
            drill_file.to_inch()
        except Exception:
            pass

        hits = getattr(drill_file, "hits", [])
        for hit in hits:
            x = y = d = None
            try:
                if hasattr(hit, "x") and hasattr(hit, "y"):
                    x = float(hit.x)
                    y = float(hit.y)
                elif hasattr(hit, "position"):
                    px, py = hit.position  # type: ignore[attr-defined]
                    x = float(px)
                    y = float(py)
                tool = getattr(hit, "tool", None)
                if tool is not None and hasattr(tool, "diameter"):
                    d = float(tool.diameter)
            except Exception:
                pass

            if x is None or y is None or d is None:
                try:
                    tool, (px, py) = hit  # type: ignore[misc]
                    x = float(px)
                    y = float(py)
                    d = float(tool.diameter)
                except Exception:
                    continue

            drills.append(
                DrillHole(
                    x_mm=x * _INCH_TO_MM,
                    y_mm=y * _INCH_TO_MM,
                    diameter_mm=d * _INCH_TO_MM,
                )
            )

    return drills


@register_check("drill_to_drill_spacing")
def run_drill_to_drill_spacing(ctx: CheckContext) -> CheckResult:
    """
    Minimum spacing between plated drills.

    spacing = center_distance - (r1 + r2)

    Units: mm.
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "mm"))
    units = "mm" if units_raw in (None, "", "mm", "um") else units_raw

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.25))  # mm
    absolute_min = float(limits.get("absolute_min", 0.20))        # mm

    drills = _collect_drills(ctx)
    if len(drills) < 2:
        viol = Violation(
            severity="warning",
            message="Not enough drills to compute drill to drill spacing.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity,
            status="warning",
            score=50.0,
            metric={
                "kind": "geometry",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    min_spacing: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    # Build grid index of drills
    max_d = max(h.diameter_mm for h in drills)
    # cell size: big enough that "likely nearest" lives nearby, but not so big that bins explode
    cell = max(0.5, recommended_min + max_d)  # mm; 0.5mm floor keeps bins reasonable on tiny thresholds

    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for idx, h in enumerate(drills):
        grid[_cell_key(h.x_mm, h.y_mm, cell)].append(idx)

    min_spacing: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    n = len(drills)

    # For correctness: for each drill i, search neighboring cells in expanding rings until we can prove
    # no unsearched cell can contain a closer candidate than current global best.
    global_best_center = math.inf  # best center-to-center distance found so far

    for i in range(n):
        h1 = drills[i]
        r1 = 0.5 * h1.diameter_mm
        ci, cj = _cell_key(h1.x_mm, h1.y_mm, cell)

        # ring expansion
        ring = 0
        local_best_center = math.inf

        while True:
            # Search this ring of cells
            for di in range(-ring, ring + 1):
                for dj in range(-ring, ring + 1):
                    # only perimeter of square ring (avoid repeats)
                    if ring > 0 and (abs(di) != ring and abs(dj) != ring):
                        continue

                    bucket = grid.get((ci + di, cj + dj))
                    if not bucket:
                        continue

                    for j in bucket:
                        if j <= i:
                            continue
                        h2 = drills[j]
                        r2 = 0.5 * h2.diameter_mm

                        dx = h2.x_mm - h1.x_mm
                        dy = h2.y_mm - h1.y_mm
                        center_dist = sqrt(dx * dx + dy * dy)

                        if center_dist < local_best_center:
                            local_best_center = center_dist

                        spacing = center_dist - (r1 + r2)
                        if spacing <= 0.0:
                            continue

                        if min_spacing is None or spacing < min_spacing:
                            min_spacing = spacing
                            global_best_center = min(global_best_center, center_dist)
                            mx = 0.5 * (h1.x_mm + h2.x_mm)
                            my = 0.5 * (h1.y_mm + h2.y_mm)
                            worst_location = ViolationLocation(
                                layer="DrillPlated",
                                x_mm=mx,
                                y_mm=my,
                                notes="Minimum spacing between two plated drill holes.",
                            )

            # Stopping condition:
            # If we already have a global best center distance, and the minimum possible distance to any
            # yet-unsearched ring is >= that, then no closer pair exists (for this i).
            if global_best_center < math.inf:
                next_ring = ring + 1
                # Minimum possible distance from h1 cell to any cell on the next ring:
                min_lb = math.inf
                for di in (-next_ring, next_ring):
                    for dj in range(-next_ring, next_ring + 1):
                        min_lb = min(min_lb, _min_possible_center_dist_between_cells(di, dj, cell))
                for dj in (-next_ring, next_ring):
                    for di in range(-next_ring, next_ring + 1):
                        min_lb = min(min_lb, _min_possible_center_dist_between_cells(di, dj, cell))

                if min_lb >= global_best_center:
                    break

            # If we still haven't found any candidate for this i, we must expand at least a bit.
            ring += 1

            # Safety: avoid infinite loops on pathological bounds
            if ring > 200:
                break

    if min_spacing is None:
        viol = Violation(
            severity="warning",
            message="All drills appear touching/overlapping; cannot compute positive drill spacing.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity,
            status="warning",
            score=50.0,
            metric={
                "kind": "geometry",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    if min_spacing < absolute_min:
        status = "fail"
        severity = "error"
    elif min_spacing < recommended_min:
        status = "warning"
        severity = "warning"
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"

    if min_spacing >= recommended_min:
        score = 100.0
    elif min_spacing <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_spacing - absolute_min) / span))

    margin_to_limit = float(min_spacing - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Minimum drill to drill spacing {min_spacing:.3f} mm is below "
            f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
        )
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=worst_location,
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status=status,
        score=score,
        metric={
            "kind": "geometry",
            "units": units,
            "measured_value": float(min_spacing),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
