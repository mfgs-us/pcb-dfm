from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation
from ..geometry import queries

# pcb-tools excellon reader (optional)
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


def _collect_drills_from_excellon(ctx: CheckContext) -> List[DrillHole]:
    """Collect plated drills from Excellon files if available."""
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
            # assume already inch
            pass

        hits = getattr(drill_file, "hits", [])
        for hit in hits:
            x = y = d = None
            # new-style DrillHit
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

            # old-style (tool, (x, y)) tuple
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


@register_check("min_annular_ring")
def run_min_annular_ring(ctx: CheckContext) -> CheckResult:
    """
    Estimate minimum annular ring for plated drills.

    Approach (approximate, Gerber-only):
      - Collect drills from Excellon files (center + diameter).
      - For each drill, find copper polygons whose bounding boxes contain the drill center.
      - Approximate pad outer radius as half of the smaller bbox dimension.
      - Annular ring = outer_radius - drill_radius.
      - Report minimum ring across all found pad/drill combinations.

    Units: mm.
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "mm"))
    units = "mm" if units_raw in (None, "", "mm", "um") else units_raw

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.1))   # mm
    absolute_min = float(limits.get("absolute_min", 0.075))       # mm

    drills = _collect_drills_from_excellon(ctx)
    copper_layers = queries.get_copper_layers(ctx.geometry)

    if not drills or not copper_layers:
        viol = Violation(
            severity="warning",
            message="Cannot compute annular ring (missing drills or copper geometry).",
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

    min_ring: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    for hole in drills:
        r_drill = hole.diameter_mm * 0.5

        for layer in copper_layers:
            for poly in layer.polygons:
                b = poly.bounds()
                # quick containment check: drill center inside copper bbox
                if (
                    hole.x_mm < b.min_x
                    or hole.x_mm > b.max_x
                    or hole.y_mm < b.min_y
                    or hole.y_mm > b.max_y
                ):
                    continue

                width = max(b.max_x - b.min_x, 0.0)
                height = max(b.max_y - b.min_y, 0.0)
                if width <= 0.0 or height <= 0.0:
                    continue

                outer_radius = 0.5 * min(width, height)
                ring = outer_radius - r_drill
                if ring < 0.0:
                    ring = 0.0

                if min_ring is None or ring < min_ring:
                    min_ring = ring
                    worst_location = ViolationLocation(
                        layer=layer.logical_layer,
                        x_mm=hole.x_mm,
                        y_mm=hole.y_mm,
                        notes="Approximate annular ring using copper pad bounding box.",
                    )

    if min_ring is None:
        viol = Violation(
            severity="warning",
            message="No copper pads found around drills to compute annular ring.",
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

    # status
    if min_ring < absolute_min:
        status = "fail"
        severity = "error"
    elif min_ring < recommended_min:
        status = "warning"
        severity = "warning"
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"

    # score
    if min_ring >= recommended_min:
        score = 100.0
    elif min_ring <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_ring - absolute_min) / span))

    margin_to_limit = float(min_ring - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Minimum annular ring {min_ring:.3f} mm is below "
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
            "measured_value": float(min_ring),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
