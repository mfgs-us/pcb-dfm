from __future__ import annotations

from math import hypot
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation, ViolationLocation

try:
    import gerber
    from gerber.primitives import Arc, Line
except Exception:  # pragma: no cover - defensive
    gerber = None
    Line = None  # type: ignore
    Arc = None  # type: ignore

# Angle below which a vertex is treated as "straight" (no real corner).
_STRAIGHT_TURN_DEG = 20.0


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    metric_cfg = ctx.check_def.metric or {}
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}
    target_min = float(target_cfg.get("min", 0.4))
    limit_min = float(limits_cfg.get("min", 0.3))
    return target_min, limit_min


def _severity(ctx: CheckContext) -> str:
    return (
        ctx.check_def.raw.get("severity_default")
        or ctx.check_def.severity
        or "info"
    )


def _key(x: float, y: float) -> Tuple[float, float]:
    # Snap coordinates so shared endpoints of adjacent segments line up.
    return (round(float(x), 4), round(float(y), 4))


def _na(ctx: CheckContext, target_min: float, limit_min: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.geometry_mm(None, target_mm=target_min, limit_low_mm=limit_min),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


def _collect_edges(ctx: CheckContext):
    """Collect outline edges from the board outline / mechanical layers.

    Returns a list of edges: (p_start, p_end, kind, radius, direction).
    kind is 'line' or 'arc'. All coordinates in mm.
    """
    edges = []
    for f in ctx.ingest.files:
        if f.layer_type not in ("outline", "mechanical"):
            continue
        try:
            layer = gerber.read(str(f.path))
        except Exception:
            continue
        try:
            layer.to_metric()
        except Exception:
            pass
        for prim in getattr(layer, "primitives", []) or []:
            try:
                if Line is not None and isinstance(prim, Line):
                    edges.append((tuple(prim.start), tuple(prim.end), "line", None, None))
                elif Arc is not None and isinstance(prim, Arc):
                    edges.append((
                        tuple(prim.start),
                        tuple(prim.end),
                        "arc",
                        float(prim.radius),
                        getattr(prim, "direction", None),
                    ))
            except Exception:
                continue
    return edges


def _build_loop(edges):
    """Chain edges into an ordered vertex loop.

    Returns (vertices, edge_infos) where vertices[i] -> vertices[i+1] is
    described by edge_infos[i] = (kind, radius, direction). The loop is
    closed (last vertex connects back to first). Returns None if the edges
    do not form a single closed contour.
    """
    if len(edges) < 3:
        return None

    # adjacency: point key -> list of (edge_index, other_point, forward)
    adj = {}
    for idx, (p0, p1, kind, radius, direction) in enumerate(edges):
        adj.setdefault(_key(*p0), []).append((idx, p1, True))
        adj.setdefault(_key(*p1), []).append((idx, p0, False))

    # A clean closed contour has exactly two incident edge-ends per vertex.
    for k, lst in adj.items():
        if len(lst) != 2:
            return None

    used = [False] * len(edges)
    start_pt = edges[0][0]
    cur = start_pt
    vertices = [tuple(cur)]
    edge_infos = []

    for _ in range(len(edges)):
        candidates = adj.get(_key(*cur), [])
        nxt = None
        for edge_index, other_pt, forward in candidates:
            if not used[edge_index]:
                nxt = (edge_index, other_pt, forward)
                break
        if nxt is None:
            return None
        edge_index, other_pt, forward = nxt
        used[edge_index] = True
        _, _, kind, radius, direction = edges[edge_index]
        edge_infos.append((kind, radius, direction, forward))
        cur = other_pt
        if _key(*cur) == _key(*start_pt):
            break
        vertices.append(tuple(cur))

    if not all(used):
        return None
    if _key(*cur) != _key(*start_pt):
        return None
    return vertices, edge_infos


def _signed_area(vertices) -> float:
    a = 0.0
    n = len(vertices)
    for i in range(n):
        x0, y0 = vertices[i]
        x1, y1 = vertices[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return 0.5 * a


@register_check("fillet_radius_milling")
def run_fillet_radius_milling(ctx: CheckContext) -> CheckResult:
    """Internal (concave) corner radius on the board outline / milled cutouts.

    Heuristic (artwork-only):
      - Chain the outline into an ordered closed loop.
      - Determine loop orientation (signed area).
      - INTERNAL corners are the concave vertices (the ones a router bit must
        round). A sharp concave line-to-line corner has an effective radius of
        0 (a bit physically cannot cut it). A concave arc contributes its own
        radius.
      - The measured value is the smallest internal corner radius.
    External (convex) corners are ignored: a router cuts those freely.
    If the outline has no concave corners (e.g. a plain convex rectangle) or
    cannot be analysed, the check is not applicable rather than fabricated.
    """
    target_min, limit_min = _thresholds(ctx)

    if gerber is None:
        return _na(ctx, target_min, limit_min,
                   "Gerber parser unavailable; cannot analyse outline corners.")

    edges = _collect_edges(ctx)
    if not edges:
        return _na(ctx, target_min, limit_min,
                   "No board outline / mechanical geometry found; corner radius not applicable.")

    try:
        loop = _build_loop(edges)
    except Exception:
        loop = None
    if loop is None:
        return _na(ctx, target_min, limit_min,
                   "Board outline is not a single closed contour; internal corner radius could not be measured.")

    vertices, edge_infos = loop
    n = len(vertices)
    if n < 3:
        return _na(ctx, target_min, limit_min,
                   "Board outline has too few segments to evaluate corners.")

    area = _signed_area(vertices)
    ccw = area > 0.0  # counter-clockwise traversal when True

    import math
    # d_in and d_out are travel directions; a near-straight vertex has them
    # nearly parallel (dot ~ +1). Skip vertices whose deflection is below the
    # threshold, i.e. dot >= cos(threshold).
    straight_cos = math.cos(math.radians(_STRAIGHT_TURN_DEG))

    # candidate internal (concave) corner radii, with a representative location
    candidates: List[Tuple[float, float, float]] = []  # (radius, x, y)

    for i in range(n):
        # Edge arriving at vertex i is edge_infos[i-1]; edge leaving is edge_infos[i].
        in_kind, in_radius, in_dir, _ = edge_infos[(i - 1) % n]
        out_kind, out_radius, out_dir, _ = edge_infos[i]
        vx, vy = vertices[i]

        if in_kind == "line" and out_kind == "line":
            px, py = vertices[(i - 1) % n]
            nx, ny = vertices[(i + 1) % n]
            d_in = (vx - px, vy - py)
            d_out = (nx - vx, ny - vy)
            li = hypot(*d_in)
            lo = hypot(*d_out)
            if li <= 1e-9 or lo <= 1e-9:
                continue
            d_in = (d_in[0] / li, d_in[1] / li)
            d_out = (d_out[0] / lo, d_out[1] / lo)
            dot = d_in[0] * d_out[0] + d_in[1] * d_out[1]
            if dot >= straight_cos:
                continue  # essentially straight, not a corner
            cross = d_in[0] * d_out[1] - d_in[1] * d_out[0]
            # Concave (internal) corner: turns opposite to the traversal winding.
            concave = (cross < 0.0) if ccw else (cross > 0.0)
            if concave:
                candidates.append((0.0, vx, vy))  # sharp internal corner: radius 0

    # Arcs: classify concavity from arc direction vs loop orientation.
    for i in range(n):
        kind, radius, direction, forward = edge_infos[i]
        if kind != "arc" or radius is None:
            continue
        # pcb-tools arc.direction is defined in the primitive's own start->end
        # sense; account for the traversal direction of this edge in the loop.
        d = direction
        if not forward:
            d = "clockwise" if d == "counterclockwise" else "counterclockwise"
        # For CCW loop, an internal (concave) corner is a clockwise arc.
        concave = (d == "clockwise") if ccw else (d == "counterclockwise")
        if concave:
            sx, sy = vertices[i]
            candidates.append((float(radius), sx, sy))

    if not candidates:
        return _na(ctx, target_min, limit_min,
                   "Board outline has no internal (concave) corners; milling corner radius not applicable.")

    min_radius, mx, my = min(candidates, key=lambda c: c[0])

    if min_radius < limit_min:
        status = "fail"
    elif min_radius < target_min:
        status = "warning"
    else:
        status = "pass"

    if min_radius >= target_min:
        score = 100.0
    elif min_radius <= limit_min:
        score = 0.0
    else:
        span = max(1e-9, target_min - limit_min)
        score = max(0.0, min(100.0, 100.0 * (min_radius - limit_min) / span))

    violations: List[Violation] = []
    if status != "pass":
        detail = "sharp (zero-radius) internal corner" if min_radius <= 1e-9 else f"{min_radius:.3f} mm radius"
        violations.append(Violation(
            severity=_severity(ctx),
            message=(
                f"Smallest internal corner is a {detail}, below recommended "
                f"{target_min:.3f} mm (absolute minimum {limit_min:.3f} mm) for milling."
            ),
            location=ViolationLocation(
                layer="Outline",
                x_mm=mx,
                y_mm=my,
                notes="Tightest internal (concave) corner on the board outline.",
            ),
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(min_radius),
            target_mm=target_min,
            limit_low_mm=limit_min,
        ),
        violations=violations,
    ).finalize()
