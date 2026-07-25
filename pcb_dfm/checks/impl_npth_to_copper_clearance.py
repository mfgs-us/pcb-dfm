"""NPTH-to-copper clearance -- does copper keep clear of non-plated holes?

A non-plated hole (mounting hole, tooling hole, unplated slot) has no plated
barrel to bond copper to the drilled wall, and its drill tolerance is looser
than a via's. Copper that comes up to -- or into -- such a hole is a genuine
defect: the drill can nick or lift the copper, and the bare wall can short to a
screw head or standoff.

This is the companion to `via_to_copper_clearance`, which deliberately filters
to *plated* drills (a via's antipad is a different, electrical rule). Here we
look at the *non-plated* holes that check excludes.

Screen, not a definitive check. Without design data we cannot tell an
*intentional* grounding ring around a mounting hole (copper you contact with a
standoff on purpose) from a stray trace or an unretracted pour. We resolve the
common false positive the same way the via check does: a small pad-sized copper
feature touching the hole is treated as the hole's own ring and skipped; only a
larger copper region (a long trace, or a pour the bare hole is drilled through)
within the clearance limit can hard-fail. Everything else is surfaced as an
advisory warning.
"""

from __future__ import annotations

import math
from collections import defaultdict
from math import floor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, excellon_hits_mm
from ..geometry.primitives import Bounds
from ..ingest import GerberFileInfo
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from ._geometry_guard import implausible_extent_result
from .impl_min_annular_ring import (
    _min_distance_to_polygon_edges,
    _point_in_polygon,
)

MAX_REPORTED_VIOLATIONS = 100

# A copper feature no larger than this (max bbox extent) that touches or contains
# the hole is treated as the hole's own intentional ring/pad, not a clearance
# violation. Same discriminator used by via_to_copper_clearance (#15).
OWN_RING_MAX_EXTENT_MM = 3.0


def _na(ctx: CheckContext, msg: str, recommended_min: float, absolute_min: float) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity="info",
        status="not_applicable",
        score=None,
        metric=MetricResult(
            kind="geometry",
            units="mm",
            measured_value=None,
            target=recommended_min,
            limit_low=absolute_min,
            limit_high=None,
            margin_to_limit=None,
        ),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


@register_check("npth_to_copper_clearance")
def run_npth_to_copper_clearance(ctx: CheckContext) -> CheckResult:
    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.25))
    absolute_min = float(limits.get("absolute_min", 0.15))

    guard = implausible_extent_result(ctx)
    if guard is not None:
        return guard

    if not GERBONARA_AVAILABLE:
        return _na(ctx, "Gerber parser unavailable; cannot evaluate NPTH-to-copper clearance.",
                   recommended_min, absolute_min)

    npth_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files
        if f.layer_type == "drill" and getattr(f, "is_plated", None) is False
    ]
    if not npth_files:
        return _na(ctx, "No non-plated (NPTH) drill layer present; nothing to evaluate.",
                   recommended_min, absolute_min)

    copper_layers = queries.get_copper_layers(ctx.geometry)
    if not copper_layers:
        return _na(ctx, "No copper layers found; NPTH-to-copper clearance not evaluable.",
                   recommended_min, absolute_min)

    holes: List[Tuple[float, float, float]] = []  # (cx, cy, radius)
    for info in npth_files:
        for h in excellon_hits_mm(Path(info.path)):
            holes.append((h.x_mm, h.y_mm, h.diameter_mm / 2.0))
    if not holes:
        return _na(ctx, "NPTH layer present but carries no holes; not evaluable.",
                   recommended_min, absolute_min)

    # (layer_name, polygon, bounds, max_extent)
    copper_entries: List[Tuple[str, object, Bounds, float]] = []
    for layer in copper_layers:
        lname = layer.logical_layer
        for poly in layer.polygons:
            b: Bounds = poly.bounds()
            extent = max(b.max_x - b.min_x, b.max_y - b.min_y)
            copper_entries.append((lname, poly, b, extent))
    if not copper_entries:
        return _na(ctx, "No copper polygons found; NPTH-to-copper clearance not evaluable.",
                   recommended_min, absolute_min)

    # Spatial grid over copper bboxes; cell sized to the search radius so runtime
    # tracks local copper density, not board area (mirrors via_to_copper).
    cell = max(0.25, min(1.0, recommended_min * 2.0))
    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for idx, (_, _poly, b, _) in enumerate(copper_entries):
        ix0, ix1 = int(floor(b.min_x / cell)), int(floor(b.max_x / cell))
        iy0, iy1 = int(floor(b.min_y / cell)), int(floor(b.max_y / cell))
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                grid[(ix, iy)].append(idx)

    min_clear: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None
    # Smallest clearance to a *large* copper region -- the unambiguous case that
    # is allowed to hard-fail (bare hole drilled through/into a pour or trace).
    min_clear_hard: Optional[float] = None
    # (clearance, layer_name, hole_x, hole_y)
    offenders: List[Tuple[float, str, float, float]] = []

    for (cx, cy, r) in holes:
        # Copper within (recommended_min + hole radius) of the hole centre can
        # matter; search that neighbourhood plus a safety margin.
        search_radius = r + recommended_min + 0.05
        ci, cj = int(floor(cx / cell)), int(floor(cy / cell))
        dk = int(math.ceil(search_radius / cell))
        seen: set[int] = set()

        for di in range(-dk, dk + 1):
            for dj in range(-dk, dk + 1):
                bucket = grid.get((ci + di, cj + dj))
                if not bucket:
                    continue
                for idx in bucket:
                    if idx in seen:
                        continue
                    seen.add(idx)
                    layer_name, poly, b, extent = copper_entries[idx]

                    # bbox lower-bound prune: true edge is at least as far as the
                    # bbox, so a bbox already beyond the window cannot matter.
                    # (A pour whose bbox contains the hole has dist 0 -> kept.)
                    if _center_to_bbox_dist(cx, cy, b) - r > search_radius:
                        continue

                    inside = _point_in_polygon(cx, cy, poly.vertices)
                    edge_to_center = _min_distance_to_polygon_edges(cx, cy, poly.vertices)
                    if not math.isfinite(edge_to_center):
                        continue

                    own_ring = extent <= OWN_RING_MAX_EXTENT_MM

                    if inside:
                        # Hole centre sits inside this copper: either its own
                        # grounding ring (fine) or a pour drilled through with no
                        # clearance (a real defect).
                        if own_ring:
                            continue
                        clearance = 0.0
                        hard = True
                    else:
                        clearance = edge_to_center - r
                        if clearance <= 0.0:
                            # Copper overlaps the bare hole. A small pad is the
                            # hole's own ring; a larger feature is a genuine hit.
                            if own_ring:
                                continue
                            clearance = 0.0
                            hard = True
                        else:
                            hard = not own_ring

                    if hard and (min_clear_hard is None or clearance < min_clear_hard):
                        min_clear_hard = clearance

                    if min_clear is None or clearance < min_clear:
                        min_clear = clearance
                        worst_location = ViolationLocation(
                            layer=layer_name,
                            x_mm=cx,
                            y_mm=cy,
                            notes="Non-plated hole centre; clearance measured to nearest copper edge.",
                        )

                    if clearance < recommended_min:
                        offenders.append((clearance, layer_name, cx, cy))

    if min_clear is None:
        # We searched a window of (hole radius + recommended_min) around every
        # hole and found no copper inside it (or only the holes' own rings). That
        # is the desired PASS state -- copper is comfortably clear of every
        # non-plated hole -- not "not applicable". Report the verified floor.
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="pass",
            severity="info",
            score=100.0,
            metric=MetricResult.geometry_mm(
                measured_mm=float(recommended_min),
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[Violation(
                severity="info",
                message=(
                    f"All copper is at least the recommended {recommended_min:.3f} mm "
                    f"clear of every non-plated hole."
                ),
                location=None,
            )],
        ).finalize()

    # Hard-fail only the unambiguous case: a bare hole drilled into/through a
    # copper region larger than an intentional ring, within the absolute limit.
    # Everything else is advisory -- an intentional grounding ring we cannot
    # distinguish from a defect without design data reads as a warning at most.
    if min_clear_hard is not None and min_clear_hard < absolute_min:
        status = "fail"
    elif min_clear < recommended_min:
        status = "warning"
    else:
        status = "pass"

    if min_clear >= recommended_min:
        score = 100.0
    elif min_clear <= absolute_min:
        score = 0.0
    else:
        span = max(1e-12, recommended_min - absolute_min)
        score = max(0.0, min(100.0, 100.0 * (min_clear - absolute_min) / span))

    violations: List[Violation] = []
    if status != "pass":
        for clearance, layer_name, hx, hy in sorted(offenders, key=lambda t: t[0])[:MAX_REPORTED_VIOLATIONS]:
            violations.append(
                Violation(
                    severity=ctx.check_def.severity,
                    message=(
                        f"Non-plated hole to copper clearance {clearance:.3f} mm on layer "
                        f"{layer_name} is below recommended {recommended_min:.3f} mm "
                        f"(absolute minimum {absolute_min:.3f} mm). Pull copper back from "
                        f"the mounting/tooling hole."
                    ),
                    location=ViolationLocation(
                        layer=layer_name, x_mm=hx, y_mm=hy,
                        notes="Non-plated hole centre; clearance measured to nearest copper edge.",
                    ),
                )
            )
        if not violations and worst_location is not None:
            violations.append(
                Violation(
                    severity=ctx.check_def.severity,
                    message=(
                        f"Minimum NPTH-to-copper clearance {min_clear:.3f} mm is below "
                        f"recommended {recommended_min:.3f} mm."
                    ),
                    location=worst_location,
                )
            )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",  # overridden by finalize()
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(min_clear),
            target_mm=recommended_min,
            limit_low_mm=absolute_min,
        ),
        violations=violations,
    ).finalize()


def _center_to_bbox_dist(cx: float, cy: float, b: Bounds) -> float:
    """Distance from point (cx,cy) to bbox b; 0 if inside."""
    dx = max(b.min_x - cx, 0.0, cx - b.max_x)
    dy = max(b.min_y - cy, 0.0, cy - b.max_y)
    return math.hypot(dx, dy)
