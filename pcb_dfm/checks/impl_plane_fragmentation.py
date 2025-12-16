from __future__ import annotations

import math
from collections import defaultdict
from math import floor
from typing import List, Tuple, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation

MAX_REPORTED_FRAGMENTS = 50


def _poly_area_mm2(poly) -> float:
    if hasattr(poly, "area_mm2"):
        return float(poly.area_mm2)
    if hasattr(poly, "area"):
        try:
            return float(poly.area())
        except TypeError:
            return float(poly.area)
    b = poly.bounds()
    return max(0.0, (b.max_x - b.min_x) * (b.max_y - b.min_y))


def _bbox_distance_mm(b1, b2) -> float:
    dx = max(0.0, max(b1.min_x - b2.max_x, b2.min_x - b1.max_x))
    dy = max(0.0, max(b1.min_y - b2.max_y, b2.min_y - b1.max_y))
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return math.hypot(dx, dy)


@register_check("plane_fragmentation")
def run_plane_fragmentation(ctx: CheckContext) -> CheckResult:
    """
    Detect small fragmented copper islands in plane like regions.

    Metric is the COUNT of such islands, to match the JSON config:
      - metric.kind: "count"
      - metric.units: "islands"
      - target.max: typically 0
      - limits.max: e.g. 10
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "islands")

    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    # Number of islands preferred to be <= target_max, with absolute limit_max
    target_max = float(target_cfg.get("max", 0.0))
    limit_max = float(limits_cfg.get("max", 10.0))

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}

    plane_poly_min_area_mm2 = float(raw_cfg.get("plane_poly_min_area_mm2", 1.0))
    connectivity_gap_mm = float(raw_cfg.get("connectivity_gap_mm", 0.05))

    fragment_max_area_mm2 = float(raw_cfg.get("fragment_max_area_mm2", 2.0))
    fragment_max_diag_mm = float(raw_cfg.get("fragment_max_diag_mm", 1.5))
    # "really tiny" fragment threshold for stronger concern (currently not used for scoring)
    tiny_fragment_area_mm2 = float(raw_cfg.get("tiny_fragment_area_mm2", 0.5))

    geom = ctx.geometry

    # Collect plane like polygons
    plane_polys: List[Tuple[object, str]] = []  # (poly, layer_name)
    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type != "copper":
            continue
        layer_name = getattr(layer, "logical_layer", None) or getattr(layer, "name", None)
        for poly in getattr(layer, "polygons", []):
            area = _poly_area_mm2(poly)
            if area >= plane_poly_min_area_mm2:
                plane_polys.append((poly, layer_name))

    if not plane_polys:
        viol = Violation(
            severity="info",
            message="No plane like copper regions detected; skipping plane fragmentation check.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity or ctx.check_def.severity_default,
            status="pass",
            score=100.0,
            metric={
                "kind": "count",
                "units": units,
                "measured_value": 0,
                "target": target_max,
                "limit_low": None,
                "limit_high": limit_max,
                "margin_to_limit": limit_max,
            },
            violations=[viol],
        )

    # Build connectivity graph
    n = len(plane_polys)
    bboxes = [poly.bounds() for poly, _ in plane_polys]
    adj: List[List[int]] = [[] for _ in range(n)]

    # Better spatial index:
    # - Use a moderate cell size so we do not collapse everything into one cell.
    # - Insert each bbox into every cell it overlaps (not just its center).
    # This keeps neighbor queries local even if some plane polygons are huge.
    cell = max(2.0, fragment_max_diag_mm * 2.0)  # mm, reasonable default
    grid = defaultdict(list)

    for i, b in enumerate(bboxes):
        ix0 = int(floor(b.min_x / cell))
        ix1 = int(floor(b.max_x / cell))
        iy0 = int(floor(b.min_y / cell))
        iy1 = int(floor(b.max_y / cell))
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                grid[(ix, iy)].append(i)

    for i, b1 in enumerate(bboxes):
        ix0 = int(floor(b1.min_x / cell))
        ix1 = int(floor(b1.max_x / cell))
        iy0 = int(floor(b1.min_y / cell))
        iy1 = int(floor(b1.max_y / cell))

        seen: set[int] = set()
        for iy in range(iy0 - 1, iy1 + 2):
            for ix in range(ix0 - 1, ix1 + 2):
                for j in grid.get((ix, iy), []):
                    if j <= i or j in seen:
                        continue
                    seen.add(j)
                    if _bbox_distance_mm(b1, bboxes[j]) <= connectivity_gap_mm:
                        adj[i].append(j)
                        adj[j].append(i)

    # Connected components
    visited = [False] * n
    components: List[List[int]] = []

    for i in range(n):
        if visited[i]:
            continue
        stack = [i]
        visited[i] = True
        comp = []
        while stack:
            k = stack.pop()
            comp.append(k)
            for nb in adj[k]:
                if not visited[nb]:
                    visited[nb] = True
                    stack.append(nb)
        components.append(comp)

    # Evaluate components as possible islands
    fragments: List[dict] = []
    smallest_area = math.inf

    for comp in components:
        comp_area = 0.0
        min_x = math.inf
        max_x = -math.inf
        min_y = math.inf
        max_y = -math.inf
        any_layer_name = None

        for idx in comp:
            poly, layer_name = plane_polys[idx]
            any_layer_name = any_layer_name or layer_name
            a = _poly_area_mm2(poly)
            comp_area += a
            b = poly.bounds()
            min_x = min(min_x, b.min_x)
            max_x = max(max_x, b.max_x)
            min_y = min(min_y, b.min_y)
            max_y = max(max_y, b.max_y)

        if not math.isfinite(min_x) or not math.isfinite(max_x):
            continue

        dx = max_x - min_x
        dy = max_y - min_y
        diag = math.hypot(dx, dy)

        # Fragment definition
        if comp_area < fragment_max_area_mm2 and diag < fragment_max_diag_mm:
            cx = 0.5 * (min_x + max_x)
            cy = 0.5 * (min_y + max_y)
            fragments.append(
                {
                    "area_mm2": comp_area,
                    "diag_mm": diag,
                    "x_mm": cx,
                    "y_mm": cy,
                    "layer": any_layer_name,
                }
            )
            if comp_area < smallest_area:
                smallest_area = comp_area

    fragment_count = len(fragments)

    if fragment_count == 0:
        viol = Violation(
            severity="info",
            message="No small fragmented plane regions detected.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity or ctx.check_def.severity_default,
            status="pass",
            score=100.0,
            metric={
                "kind": "count",
                "units": units,
                "measured_value": 0,
                "target": target_max,
                "limit_low": None,
                "limit_high": limit_max,
                "margin_to_limit": limit_max,
            },
            violations=[viol],
        )

    measured_value = fragment_count

    if measured_value <= target_max:
        status = "pass"
        severity = ctx.check_def.severity or ctx.check_def.severity_default or "warning"
        score = 100.0
    elif measured_value <= limit_max:
        status = "warning"
        severity = "warning"
        # Basic linear score between target_max and limit_max, mapped to [pass_threshold, 100]
        # For now, we just scale 100 -> 60 as we approach limit_max.
        span = max(1e-6, limit_max - target_max)
        frac = (limit_max - measured_value) / span
        score = max(0.0, min(100.0, 60.0 + 40.0 * max(0.0, frac)))
    else:
        status = "fail"
        severity = "error"
        score = 0.0

    margin_to_limit = float(limit_max - measured_value)

    # Sort fragments from smallest area (worst) to largest
    fragments_sorted = sorted(fragments, key=lambda f: f["area_mm2"])

    violations: List[Violation] = []
    for idx, frag in enumerate(fragments_sorted[:MAX_REPORTED_FRAGMENTS]):
        area = frag["area_mm2"]
        layer = frag["layer"]
        x = frag["x_mm"]
        y = frag["y_mm"]

        if idx == 0:
            msg = (
                f"Detected {fragment_count} small fragmented plane region(s); "
                f"this fragment area is {area:.3f} mm² "
                f"(smallest fragment area is {smallest_area:.3f} mm²)."
            )
        else:
            msg = f"Small fragmented plane region with area {area:.3f} mm²."

        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=ViolationLocation(
                    layer=layer,
                    x_mm=x,
                    y_mm=y,
                    notes="Small isolated copper island in plane like region.",
                ),
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity or ctx.check_def.severity_default,
        status=status,
        score=score,
        metric={
            "kind": "count",
            "units": units,
            "measured_value": measured_value,
            "target": target_max,
            "limit_low": None,
            "limit_high": limit_max,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
