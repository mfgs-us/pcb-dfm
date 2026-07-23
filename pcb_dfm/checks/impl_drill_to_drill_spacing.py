from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import floor, hypot, sqrt
from typing import Dict, List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import excellon_hits_mm
from ..results import CheckResult, MetricResult, Violation, ViolationLocation


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
    """All drilled holes in mm, via the gerbonara parse backend (#3).

    Replaces the pcb-tools path, which called ``to_inch()`` then multiplied by
    25.4 and so double-converted **mm-native** Excellon files (holes landed
    25.4x off the board).
    """
    drills: List[DrillHole] = []
    for f in ctx.ingest.files:
        if f.layer_type != "drill":
            continue
        for hit in excellon_hits_mm(f.path):
            drills.append(DrillHole(
                x_mm=hit.x_mm, y_mm=hit.y_mm, diameter_mm=hit.diameter_mm,
            ))
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
            severity="info",
            message=(
                "Fewer than two drilled holes, so there is no hole pair to measure; "
                "drill-to-drill spacing not applicable."
            ),
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",  # Default value, will be overridden by finalize()
            score=None,
            # Reporting 0.0 mm here was actively misleading: the worst possible
            # spacing, standing in for "there was nothing to measure".
            metric=MetricResult.geometry_mm(
                measured_mm=None,
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[viol],
        ).finalize()

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

    # Only NEARBY drills can violate spacing: any pair whose centre-to-centre
    # distance exceeds recommended_min + both radii is comfortably in spec, so we
    # ignore it. With the grid cell sized to that cutoff, each drill only needs
    # to look at its own + 8 neighbouring cells -- O(n) with a small constant,
    # regardless of board size or how sparsely holes are placed. (A previous
    # expanding-ring search blew up to O(rings^3) for far-apart holes.)
    cutoff = cell  # cell == recommended_min + max_d (+ 0.5 floor); covers every violation
    for i in range(n):
        h1 = drills[i]
        r1 = 0.5 * h1.diameter_mm
        ci, cj = _cell_key(h1.x_mm, h1.y_mm, cell)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for j in grid.get((ci + di, cj + dj), ()):
                    if j <= i:
                        continue
                    h2 = drills[j]
                    r2 = 0.5 * h2.diameter_mm
                    center_dist = sqrt((h2.x_mm - h1.x_mm) ** 2 + (h2.y_mm - h1.y_mm) ** 2)
                    if center_dist > cutoff:
                        continue  # far apart -> not a spacing concern
                    # spacing <= 0 means tangent/overlapping holes -- the MOST
                    # severe drill defect. It is recorded (not skipped) so the
                    # metric reflects the true minimum, including overlaps.
                    spacing = center_dist - (r1 + r2)
                    if min_spacing is None or spacing < min_spacing:
                        min_spacing = spacing
                        worst_location = ViolationLocation(
                            layer="DrillPlated",
                            x_mm=0.5 * (h1.x_mm + h2.x_mm),
                            y_mm=0.5 * (h1.y_mm + h2.y_mm),
                            notes="Minimum spacing between two plated drill holes.",
                        )

    if min_spacing is None:
        # No pair fell within the cutoff -> every drill is comfortably far from
        # its neighbours, which is a pass (nothing to flag).
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="pass",
            severity="info",
            score=100.0,
            metric=MetricResult.geometry_mm(
                measured_mm=None,
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[],
        ).finalize()

    # Decide status only (severity handled by finalize)
    if min_spacing < absolute_min:
        status = "fail"
    elif min_spacing < recommended_min:
        status = "warning"
    else:
        status = "pass"

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
        if min_spacing <= 0.0:
            msg = (
                f"Drill holes overlap or are tangent (edge-to-edge spacing "
                f"{min_spacing:.3f} mm <= 0). Overlapping holes are a critical "
                f"drilling defect; minimum spacing must be at least "
                f"{recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
            )
        else:
            msg = (
                f"Minimum drill to drill spacing {min_spacing:.3f} mm is below "
                f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
            )
        violations.append(
            Violation(
                severity="warning" if status == "warning" else "error",
                message=msg,
                location=worst_location,
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity="info",  # Default value, will be overridden by finalize()
        status=status,
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(min_spacing),
            target_mm=recommended_min,
            limit_low_mm=absolute_min,
        ),
        violations=violations,
    ).finalize()
