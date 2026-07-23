from __future__ import annotations

from math import hypot
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, gerber_edges_mm
from ..results import CheckResult, MetricResult, Violation, ViolationLocation

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

    Sourced from the gerbonara parse backend (#3), so arcs carry their **true**
    radius -- the previous pcb-tools path chord-approximated them, which meant a
    filleted corner could lose the very radius this check measures.
    """
    edges = []
    for f in ctx.ingest.files:
        if f.layer_type not in ("outline", "mechanical"):
            continue
        edges.extend(gerber_edges_mm(f.path))
    return edges
def _build_loops(edges):
    """Chain edges into one or more ordered, closed vertex loops.

    A real board outline layer is frequently a *union* of disjoint closed
    contours: the board perimeter plus one or more internal cutouts/slots, each
    of which has its own concave corners a router bit must round. Returns a list
    of (vertices, edge_infos) loops, one per contour, where vertices[i] ->
    vertices[i+1] is described by edge_infos[i] = (kind, radius, direction,
    forward) and each loop closes back on itself.

    Returns None only when the edges do not form a clean union of closed
    contours (any vertex whose incident edge-ends != 2), so a genuinely
    ambiguous / open outline still degrades to not-applicable rather than a
    fabricated measurement.
    """
    if len(edges) < 3:
        return None

    # adjacency: point key -> list of (edge_index, other_point, forward)
    adj = {}
    for idx, (p0, p1, kind, radius, direction) in enumerate(edges):
        adj.setdefault(_key(*p0), []).append((idx, p1, True))
        adj.setdefault(_key(*p1), []).append((idx, p0, False))

    # A clean union of closed contours has exactly two incident edge-ends per
    # vertex. Anything else (dangling ends, T-junctions) is not analysable.
    for k, lst in adj.items():
        if len(lst) != 2:
            return None

    used = [False] * len(edges)
    loops = []

    for seed in range(len(edges)):
        if used[seed]:
            continue
        # Walk the contour that this unused edge belongs to.
        start_pt = edges[seed][0]
        cur = start_pt
        vertices = [tuple(cur)]
        edge_infos = []
        closed = False

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
                closed = True
                break
            vertices.append(tuple(cur))

        if not closed or len(vertices) < 3:
            return None
        loops.append((vertices, edge_infos))

    if not all(used) or not loops:
        return None
    return loops


def _signed_area(vertices) -> float:
    a = 0.0
    n = len(vertices)
    for i in range(n):
        x0, y0 = vertices[i]
        x1, y1 = vertices[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return 0.5 * a


def _point_in_ring(x: float, y: float, verts) -> bool:
    """Ray-casting point-in-polygon on a ring of (x, y) tuples."""
    inside = False
    n = len(verts)
    for i in range(n):
        xi, yi = verts[i]
        xj, yj = verts[(i + 1) % n]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi) + xi
        ):
            inside = not inside
    return inside


def _hole_flags(loops) -> List[bool]:
    """Classify each contour as a hole (material outside it) vs a solid boundary
    (material inside it) by containment parity.

    A board outline layer is a set of nested rings: the perimeter, cutouts
    inside it, islands inside cutouts, and so on. A ring nested an odd number
    of times is a hole; the concave/convex sense of its corners relative to the
    *material* is the opposite of the sense relative to its own interior. Winding
    direction alone can't tell a hole from a boss, so we test containment
    explicitly rather than trusting the artwork's arc/loop orientation."""
    flags: List[bool] = []
    for i, (vi, _) in enumerate(loops):
        px, py = vi[0]
        depth = 0
        for j, (vj, _) in enumerate(loops):
            if i == j:
                continue
            if _point_in_ring(px, py, vj):
                depth += 1
        flags.append(depth % 2 == 1)
    return flags


def _concave_corner_radii(
    vertices, edge_infos, is_hole: bool = False
) -> List[Tuple[float, float, float]]:
    """Return (radius, x, y) for every internal (concave) corner of one closed
    contour. A sharp line-to-line concave corner has an effective radius of 0;
    a concave arc contributes its own radius. External (convex) corners are
    ignored — a router cuts those freely.

    ``is_hole`` inverts the classification: for a cutout the router works the
    *outside* of the loop, so a corner that is convex with respect to the loop's
    own interior (e.g. every corner of a rectangular pocket) is concave with
    respect to the surrounding copper/material and must be rounded."""
    import math

    n = len(vertices)
    if n < 3:
        return []

    area = _signed_area(vertices)
    ccw = area > 0.0  # counter-clockwise traversal when True
    straight_cos = math.cos(math.radians(_STRAIGHT_TURN_DEG))

    candidates: List[Tuple[float, float, float]] = []

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
            if is_hole:
                concave = not concave  # material is outside a cutout loop
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
        if is_hole:
            concave = not concave  # material is outside a cutout loop
        if concave:
            sx, sy = vertices[i]
            candidates.append((float(radius), sx, sy))

    return candidates


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

    if not GERBONARA_AVAILABLE:
        return _na(ctx, target_min, limit_min,
                   "Gerber parser unavailable; cannot analyse outline corners.")

    edges = _collect_edges(ctx)
    if not edges:
        return _na(ctx, target_min, limit_min,
                   "No board outline / mechanical geometry found; corner radius not applicable.")

    try:
        loops = _build_loops(edges)
    except Exception:
        loops = None
    if loops is None:
        return _na(ctx, target_min, limit_min,
                   "Board outline is not a union of closed contours; internal corner radius could not be measured.")

    # Analyse every contour (board perimeter + each internal cutout/slot) and
    # take the tightest internal corner across all of them. Containment parity
    # tells us which contours are cutouts, so a pocket's corners are measured
    # against the surrounding material rather than the pocket's own interior.
    hole_flags = _hole_flags(loops)
    candidates: List[Tuple[float, float, float]] = []  # (radius, x, y)
    for (vertices, edge_infos), is_hole in zip(loops, hole_flags):
        candidates.extend(_concave_corner_radii(vertices, edge_infos, is_hole))

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
