from __future__ import annotations

from collections import defaultdict
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation
from ..geometry import queries

try:
    import gerber  # type: ignore
    from gerber.primitives import Line, Circle, Rectangle  # type: ignore
except Exception:
    gerber = None  # type: ignore
    Line = Circle = Rectangle = None  # type: ignore

_INCH_TO_MM = 25.4


def _primitive_bbox_inch(prim) -> Optional[Tuple[float, float, float, float]]:
    """Best-effort bounding box for common silkscreen primitives."""
    # Line segment with width
    if isinstance(prim, Line):
        (x1, y1) = prim.start
        (x2, y2) = prim.end
        w = getattr(prim, "width", 0.0) or 0.0
        half = w * 0.5
        min_x = min(x1, x2) - half
        max_x = max(x1, x2) + half
        min_y = min(y1, y2) - half
        max_y = max(y1, y2) + half
        return (min_x, max_x, min_y, max_y)

    if isinstance(prim, Circle):
        (cx, cy) = prim.position
        r = prim.radius
        return (cx - r, cx + r, cy - r, cy + r)

    if isinstance(prim, Rectangle):
        (cx, cy) = prim.position
        w = prim.width
        h = prim.height
        half_w = w * 0.5
        half_h = h * 0.5
        return (cx - half_w, cx + half_w, cy - half_h, cy + half_h)

    # polygons/regions
    verts = getattr(prim, "vertices", None)
    if verts:
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        return (min(xs), max(xs), min(ys), max(ys))

    return None


@register_check("silkscreen_on_copper")
def run_silkscreen_on_copper(ctx: CheckContext) -> CheckResult:
    """
    Detect silkscreen printed over copper.

    Approximation:
      - Parse silkscreen Gerbers (GTO/GBO) into primitive bounding boxes.
      - Use copper geometry polygons from ctx.geometry.
      - If a silkscreen bbox intersects a copper bbox on the same side,
        count it as a violation.

    Metric is discrete: number of overlaps (units: count).
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "count"))
    units = "count" if units_raw in (None, "", "count") else units_raw

    if gerber is None or Line is None:
        viol = Violation(
            severity="warning",
            message="Silkscreen parsing not available (gerber library missing).",
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
                "kind": "count",
                "units": units,
                "measured_value": None,
                "target": 0,
                "limit_low": None,
                "limit_high": 0,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Collect copper bboxes per side
    copper_layers = queries.get_copper_layers(ctx.geometry)
    copper_bboxes_by_side: dict[str, List[Tuple[float, float, float, float, str]]] = {}
    for layer in copper_layers:
        side = getattr(layer, "side", "Unknown") or "Unknown"
        for poly in layer.polygons:
            b = poly.bounds()
            copper_bboxes_by_side.setdefault(side, []).append(
                (b.min_x, b.max_x, b.min_y, b.max_y, layer.logical_layer)
            )

    cell = 1.0
    copper_grids = {}

    for side, boxes in copper_bboxes_by_side.items():
        g = defaultdict(list)
        for idx, (min_x, max_x, min_y, max_y, layer) in enumerate(boxes):
            cx = 0.5 * (min_x + max_x)
            cy = 0.5 * (min_y + max_y)
            g[(int(cx // cell), int(cy // cell))].append(idx)
        copper_grids[side] = g

    # Collect silkscreen primitives per side
    silk_bboxes_by_side: dict[str, List[Tuple[float, float, float, float, str]]] = {}
    for f in ctx.ingest.files:
        if f.layer_type not in ("silk", "silkscreen"):
            continue
        if f.format != "gerber":
            continue

        try:
            g_layer = gerber.read(str(f.path))
        except Exception:
            continue

        try:
            g_layer.to_inch()
        except Exception:
            pass

        side = f.side or "Unknown"

        for prim in getattr(g_layer, "primitives", []):
            bb_in = _primitive_bbox_inch(prim)
            if bb_in is None:
                continue
            min_x_in, max_x_in, min_y_in, max_y_in = bb_in
            silk_bboxes_by_side.setdefault(side, []).append(
                (
                    min_x_in * _INCH_TO_MM,
                    max_x_in * _INCH_TO_MM,
                    min_y_in * _INCH_TO_MM,
                    max_y_in * _INCH_TO_MM,
                    f.logical_layer,
                )
            )

    total_overlaps = 0
    violations: List[Violation] = []

    for side, silk_boxes in silk_bboxes_by_side.items():
        copper_boxes = copper_bboxes_by_side.get(side, [])
        if not copper_boxes:
            continue

        for s_min_x, s_max_x, s_min_y, s_max_y, s_layer in silk_boxes:
            ci = int((s_min_x + s_max_x) * 0.5 // cell)
            cj = int((s_min_y + s_max_y) * 0.5 // cell)

            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for idx in copper_grids[side].get((ci + di, cj + dj), []):
                        c_min_x, c_max_x, c_min_y, c_max_y, c_layer = copper_boxes[idx]
                        if (
                            s_max_x < c_min_x
                            or s_min_x > c_max_x
                            or s_max_y < c_min_y
                            or s_min_y > c_max_y
                        ):
                            continue

                        # overlap
                        total_overlaps += 1
                        cx = 0.5 * max(s_min_x, c_min_x) + 0.5 * min(s_max_x, c_max_x)
                        cy = 0.5 * max(s_min_y, c_min_y) + 0.5 * min(s_max_y, c_max_y)
                        msg = f"Silkscreen overlaps copper on side {side}."
                        violations.append(
                            Violation(
                                severity=ctx.check_def.severity or "warning",
                                message=msg,
                                location=ViolationLocation(
                                    layer=s_layer,
                                    x_mm=cx,
                                    y_mm=cy,
                                    notes=f"Silk {s_layer} over copper {c_layer}.",
                                ),
                            )
                        )
                        # do not break here; we want to count multiple overlaps

    if total_overlaps == 0:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity,
            status="pass",
            score=100.0,
            metric={
                "kind": "count",
                "units": units,
                "measured_value": 0,
                "target": 0,
                "limit_low": None,
                "limit_high": 0,
                "margin_to_limit": 0,
            },
            violations=[],
        )

    # any overlap is a warning by default
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status="warning",
        score=0.0,
        metric={
            "kind": "count",
            "units": units,
            "measured_value": total_overlaps,
            "target": 0,
            "limit_low": None,
            "limit_high": 0,
            "margin_to_limit": -float(total_overlaps),
        },
        violations=violations,
    )
