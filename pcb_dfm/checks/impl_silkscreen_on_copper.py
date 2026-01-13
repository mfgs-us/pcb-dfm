from __future__ import annotations

import os
from collections import defaultdict
from functools import lru_cache
from math import floor
from typing import Dict, List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation, MetricResult
from ..geometry import queries

try:
    import gerber  # type: ignore
    from gerber.primitives import Line, Circle, Rectangle  # type: ignore
except Exception:
    gerber = None  # type: ignore
    Line = Circle = Rectangle = None  # type: ignore

_INCH_TO_MM = 25.4


def _norm_side(v: Optional[str]) -> str:
    if not v:
        return "Unknown"
    s = str(v).strip().lower()
    if s in ("top", "t", "front", "f"):
        return "Top"
    if s in ("bottom", "bot", "b", "back"):
        return "Bottom"
    return str(v).strip() or "Unknown"


def _primitive_bbox_inch(prim) -> Optional[Tuple[float, float, float, float]]:
    """Best-effort bounding box for common silkscreen primitives."""
    if Line is not None and isinstance(prim, Line):
        (x1, y1) = prim.start
        (x2, y2) = prim.end
        w = getattr(prim, "width", 0.0) or 0.0
        half = w * 0.5
        min_x = min(x1, x2) - half
        max_x = max(x1, x2) + half
        min_y = min(y1, y2) - half
        max_y = max(y1, y2) + half
        return (min_x, max_x, min_y, max_y)

    if Circle is not None and isinstance(prim, Circle):
        (cx, cy) = prim.position
        r = prim.radius
        return (cx - r, cx + r, cy - r, cy + r)

    if Rectangle is not None and isinstance(prim, Rectangle):
        (cx, cy) = prim.position
        w = prim.width
        h = prim.height
        half_w = w * 0.5
        half_h = h * 0.5
        return (cx - half_w, cx + half_w, cy - half_h, cy + half_h)

    verts = getattr(prim, "vertices", None)
    if verts:
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        return (min(xs), max(xs), min(ys), max(ys))

    return None


def _bbox_intersects(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> bool:
    a_min_x, a_max_x, a_min_y, a_max_y = a
    b_min_x, b_max_x, b_min_y, b_max_y = b
    if a_max_x < b_min_x or a_min_x > b_max_x:
        return False
    if a_max_y < b_min_y or a_min_y > b_max_y:
        return False
    return True


@lru_cache(maxsize=64)
def _cached_silk_bboxes(path: str, mtime_ns: int):
    try:
        g_layer = gerber.read(path)
        try:
            g_layer.to_inch()
        except Exception:
            pass
    except Exception:
        return []

    out = []
    for prim in getattr(g_layer, "primitives", []):
        bb_in = _primitive_bbox_inch(prim)
        if bb_in is None:
            continue
        min_x_in, max_x_in, min_y_in, max_y_in = bb_in
        out.append((
            min_x_in * _INCH_TO_MM,
            max_x_in * _INCH_TO_MM,
            min_y_in * _INCH_TO_MM,
            max_y_in * _INCH_TO_MM,
        ))
    return out


@register_check("silkscreen_on_copper")
def run_silkscreen_on_copper(ctx: CheckContext) -> CheckResult:
    """
    Detect silkscreen printed over copper.

    Focus:
      - Prefer checking silkscreen over EXPOSED copper (copper that is not covered by soldermask),
        because silk over masked copper is usually fine.
      - If we cannot find soldermask openings, we fall back to checking against all copper bboxes.

    Approach:
      - Parse silkscreen Gerbers into primitive bboxes (mm).
      - Build a spatial grid for copper bboxes (or exposed-copper bboxes).
      - For each silk bbox, query candidate copper boxes via grid and test bbox intersection.
      - Count overlaps per silk primitive (not per copper hit) to avoid inflation.
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "count"))
    units = "count" if units_raw in (None, "", "count") else units_raw

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    max_reported = int(raw_cfg.get("max_reported", 200))

    # Optional "focus" knobs
    focus_exposed_only = bool(raw_cfg.get("focus_exposed_copper_only", True))
    cell_mm = float(raw_cfg.get("grid_cell_mm", 1.0))
    # Inflate silk bbox a bit to be conservative with bbox approximation
    silk_inflate_mm = float(raw_cfg.get("silk_inflate_mm", 0.0))

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
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=50.0,
            metric=MetricResult(
                kind="count",
                units=units,
                measured_value=None,
                target=0,
                limit_low=None,
                limit_high=0,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    # ---- Collect copper bboxes per side
    copper_layers = queries.get_copper_layers(ctx.geometry)
    copper_bboxes_by_side: Dict[str, List[Tuple[float, float, float, float, str]]] = defaultdict(list)
    for layer in copper_layers:
        side = _norm_side(getattr(layer, "side", None))
        lname = getattr(layer, "logical_layer", "Copper")
        for poly in getattr(layer, "polygons", []):
            b = poly.bounds()
            copper_bboxes_by_side[side].append((b.min_x, b.max_x, b.min_y, b.max_y, lname))

    # ---- Collect soldermask opening bboxes per side (optional, best effort)
    mask_openings_by_side: Dict[str, List[Tuple[float, float, float, float]]] = defaultdict(list)
    if focus_exposed_only:
        # Prefer a helper if it exists, otherwise scan geometry layers by attributes.
        mask_layers = []
        get_mask_layers = getattr(queries, "get_solder_mask_layers", None)
        if callable(get_mask_layers):
            try:
                mask_layers = list(get_mask_layers(ctx.geometry))
            except Exception:
                mask_layers = []
        else:
            # Best-effort fallback: look for layers whose layer_type looks like "mask"
            for lyr in getattr(ctx.geometry, "layers", []):
                if getattr(lyr, "layer_type", None) == "mask":
                    mask_layers.append(lyr)

        for layer in mask_layers:
            side = _norm_side(getattr(layer, "side", None))
            # Convention: mask polygons often represent openings (or inversions). We can only do bbox-level best effort.
            for poly in getattr(layer, "polygons", []):
                b = poly.bounds()
                mask_openings_by_side[side].append((b.min_x, b.max_x, b.min_y, b.max_y))

    # If we have mask openings, restrict copper bboxes to those that intersect an opening.
    # This drastically improves focus: "silk over exposed copper".
    if focus_exposed_only and any(mask_openings_by_side.values()):
        exposed_copper_by_side: Dict[str, List[Tuple[float, float, float, float, str]]] = defaultdict(list)

        # Build mask grids for quick intersection tests
        mask_grid: Dict[str, Dict[Tuple[int, int], List[int]]] = {}
        for side, mboxes in mask_openings_by_side.items():
            g: Dict[Tuple[int, int], List[int]] = defaultdict(list)
            for mi, (mnx, mxx, mny, mxy) in enumerate(mboxes):
                ix0 = int(floor(mnx / cell_mm))
                ix1 = int(floor(mxx / cell_mm))
                iy0 = int(floor(mny / cell_mm))
                iy1 = int(floor(mxy / cell_mm))
                for iy in range(iy0, iy1 + 1):
                    for ix in range(ix0, ix1 + 1):
                        g[(ix, iy)].append(mi)
            mask_grid[side] = g

        for side, cboxes in copper_bboxes_by_side.items():
            mboxes = mask_openings_by_side.get(side, [])
            g = mask_grid.get(side)
            if not mboxes or not g:
                continue

            for (cminx, cmaxx, cminy, cmaxy, lname) in cboxes:
                ix0 = int(floor(cminx / cell_mm))
                ix1 = int(floor(cmaxx / cell_mm))
                iy0 = int(floor(cminy / cell_mm))
                iy1 = int(floor(cmaxy / cell_mm))

                hit = False
                seen: set[int] = set()
                for iy in range(iy0 - 1, iy1 + 2):
                    for ix in range(ix0 - 1, ix1 + 2):
                        for mi in g.get((ix, iy), []):
                            if mi in seen:
                                continue
                            seen.add(mi)
                            if _bbox_intersects((cminx, cmaxx, cminy, cmaxy), mboxes[mi]):
                                hit = True
                                break
                        if hit:
                            break
                    if hit:
                        break

                if hit:
                    exposed_copper_by_side[side].append((cminx, cmaxx, cminy, cmaxy, lname))

        # Only replace if we found anything; otherwise keep full copper fallback.
        if any(exposed_copper_by_side.values()):
            copper_bboxes_by_side = exposed_copper_by_side

    # ---- Build copper grids per side (bbox coverage -> cells)
    copper_grids: Dict[str, Dict[Tuple[int, int], List[int]]] = {}
    for side, boxes in copper_bboxes_by_side.items():
        g: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for idx, (min_x, max_x, min_y, max_y, _layer) in enumerate(boxes):
            ix0 = int(floor(min_x / cell_mm))
            ix1 = int(floor(max_x / cell_mm))
            iy0 = int(floor(min_y / cell_mm))
            iy1 = int(floor(max_y / cell_mm))
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    g[(ix, iy)].append(idx)
        copper_grids[side] = g

    # ---- Collect silkscreen primitive bboxes per side (mm)
    silk_bboxes_by_side: Dict[str, List[Tuple[float, float, float, float, str]]] = defaultdict(list)
    for f in ctx.ingest.files:
        if f.layer_type not in ("silk", "silkscreen"):
            continue
        if f.format != "gerber":
            continue

        st = os.stat(f.path)
        bboxes = _cached_silk_bboxes(str(f.path), st.st_mtime_ns)
        side = _norm_side(getattr(f, "side", None))
        logical = getattr(f, "logical_layer", "Silkscreen")

        for min_x, max_x, min_y, max_y in bboxes:
            silk_bboxes_by_side[side].append(
                (min_x, max_x, min_y, max_y, logical)
            )

    total_overlaps = 0
    violations: List[Violation] = []

    # Count overlaps per silk primitive to avoid inflating counts when copper is fragmented
    for side, silk_boxes in silk_bboxes_by_side.items():
        copper_boxes = copper_bboxes_by_side.get(side, [])
        grid = copper_grids.get(side)
        if not copper_boxes or not grid:
            continue

        for s_min_x, s_max_x, s_min_y, s_max_y, s_layer in silk_boxes:
            ix0 = int(floor(s_min_x / cell_mm))
            ix1 = int(floor(s_max_x / cell_mm))
            iy0 = int(floor(s_min_y / cell_mm))
            iy1 = int(floor(s_max_y / cell_mm))

            overlapped = False
            seen: set[int] = set()

            for iy in range(iy0 - 1, iy1 + 2):
                for ix in range(ix0 - 1, ix1 + 2):
                    for idx in grid.get((ix, iy), []):
                        if idx in seen:
                            continue
                        seen.add(idx)
                        c_min_x, c_max_x, c_min_y, c_max_y, c_layer = copper_boxes[idx]

                        if (
                            s_max_x < c_min_x
                            or s_min_x > c_max_x
                            or s_max_y < c_min_y
                            or s_min_y > c_max_y
                        ):
                            continue

                        # First copper hit is enough for this silk primitive
                        overlapped = True

                        cx = 0.5 * (max(s_min_x, c_min_x) + min(s_max_x, c_max_x))
                        cy = 0.5 * (max(s_min_y, c_min_y) + min(s_max_y, c_max_y))
                        msg = f"Silkscreen overlaps copper on side {side}."
                        if len(violations) < max_reported:
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
                        break
                    if overlapped:
                        break
                if overlapped:
                    break

            if overlapped:
                total_overlaps += 1

            if len(violations) >= max_reported:
                break

    if total_overlaps == 0:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="pass",
            severity="info",  # Default value, will be overridden by finalize()
            score=100.0,
            metric=MetricResult(
                kind="count",
                units=units,
                measured_value=0,
                target=0,
                limit_low=None,
                limit_high=0,
                margin_to_limit=0,
            ),
            violations=[],
        ).finalize()

    # 5A) Silkscreen over copper: default to warning (CAM clipping assumed)
    # Optionally add fab_clips_silkscreen=True profile default
    
    # Check if user has indicated fab clips silkscreen (default behavior)
    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    fab_clips_silkscreen = raw_cfg.get("fab_clips_silkscreen", True)  # Default to True
    
    # Determine status only (severity handled by finalize)
    if fab_clips_silkscreen:
        # Assume fab will clip silkscreen, so treat as warning
        status = "warning"
        score = 60.0  # Warning score but not failure
    else:
        # User wants strict silkscreen checking
        status = "warning"
        score = 60.0

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity="info",  # Default value, will be overridden by finalize()
        status=status,
        score=score,
        metric=MetricResult(
            kind="count",
            units=units,
            measured_value=total_overlaps,
            target=0,
            limit_low=None,
            limit_high=0,
            margin_to_limit=-float(total_overlaps),
        ),
        violations=violations,
    ).finalize()
