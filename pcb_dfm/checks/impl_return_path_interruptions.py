"""
return_path_interruptions — high-speed traces crossing a plane gap/split.

A signal's return current flows in the reference plane directly beneath (or
above) the trace. Where that plane has a slot, void, or split, the return
current must detour around it — the classic source of EMI and signal-integrity
grief. This check flags high-speed traces that run *over a gap* in their
adjacent reference plane.

Built on the net-tagged geometry work (#1): the reference plane is a large
copper pour on the adjacent stack layer (net-labelled via the NetMap when
available), and the high-speed traces come from the design-data routing.

Deliberately conservative — a false `error` here is costly, so the check only
fires when it is confident, and is ``not_applicable`` otherwise:

  * needs design data with high-speed nets (diff-pair / controlled-impedance
    members, or an explicit ``raw.high_speed_net_classes`` list) that carry
    routed geometry;
  * needs >= 2 copper layers and an identifiable plane pour on the layer
    adjacent to the signal;
  * only counts a gap the trace crosses with plane present on *both* sides (a
    real slot/split), never the trace simply running off the plane; and
  * only trusts a net whose trace is mostly (>= 50%) over the plane, so we know
    that plane really is its reference.

Metric: total length (mm) of high-speed trace crossing plane gaps. target 0,
limit 10 mm (minimize) — any crossing warns, a lot fails.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..geometry.net_map import _canon_layer, get_or_build_net_map
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_min_annular_ring import _point_in_polygon

Point = Tuple[float, float]


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    m = ctx.check_def.metric or {}
    t = m.get("target") or {}
    limits = m.get("limits") or {}
    target_max = float(t.get("max", 0.0)) if isinstance(t, dict) else 0.0
    limit_max = float(limits.get("max", 10.0)) if isinstance(limits, dict) else 10.0
    return target_max, limit_max


def _na(ctx: CheckContext, target_max: float, limit_max: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.geometry_mm(None, target_mm=target_max, limit_high_mm=limit_max),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


def _high_speed_nets(dd, raw: dict) -> set:
    """Conservative high-speed set: diff-pair members + controlled-impedance
    nets + any net whose class is in ``raw.high_speed_net_classes``."""
    names: set = set()
    for dp in dd.diff_pairs:
        names.add(dp.positive)
        names.add(dp.negative)
    for ci in dd.controlled_impedance:
        names.add(ci.name)
    classes = {c.strip().lower() for c in (raw.get("high_speed_net_classes") or [])}
    if classes:
        for nm, net in dd.nets.items():
            if net.net_class and net.net_class.strip().lower() in classes:
                names.add(nm)
    return names


def _rank(canon: Optional[str]) -> int:
    if canon == "top":
        return -1
    if canon == "bottom":
        return 1_000_000
    if canon and canon.startswith("inner"):
        try:
            return int(canon[5:])
        except ValueError:
            return 500_000
    return 500_000


def _poly_pts(poly) -> List[Point]:
    return [(float(v.x), float(v.y)) for v in poly.vertices]


def _shoelace_area(pts: List[Point]) -> float:
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _scan_segment(a: Point, b: Point, planes) -> Tuple[float, float, float, List[Tuple[float, float, float]]]:
    """Walk a trace segment, sampling reference-plane coverage.

    Returns (crossing_len, covered_len, seg_len, gaps) where ``gaps`` is a list
    of (x, y, gap_len) for each *bounded* uncovered run (plane present on both
    sides = a real slot/split the trace crosses)."""
    seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
    if seg_len <= 0.0:
        return 0.0, 0.0, 0.0, []
    n = max(2, min(400, int(seg_len / 0.1) + 1))
    step = seg_len / (n - 1)

    cov: List[bool] = []
    pts: List[Point] = []
    for i in range(n):
        t = i / (n - 1)
        x = a[0] + t * (b[0] - a[0])
        y = a[1] + t * (b[1] - a[1])
        pts.append((x, y))
        cov.append(any(_point_in_polygon(x, y, p.vertices) for p in planes))

    covered_len = step * sum(1 for c in cov if c)
    crossing_len = 0.0
    gaps: List[Tuple[float, float, float]] = []
    i = 0
    while i < n:
        if not cov[i]:
            j = i
            while j < n and not cov[j]:
                j += 1
            # Bounded uncovered run: covered before (i>0) and after (j<n).
            if i > 0 and j < n:
                # Span between the bracketing covered samples -- a better
                # estimate of the true void width than the count of interior
                # samples, which under-resolves narrow slots.
                run_len = (j - i + 1) * step
                if run_len >= 0.1:
                    crossing_len += run_len
                    mx, my = pts[(i + j - 1) // 2]
                    gaps.append((mx, my, run_len))
            i = j
        else:
            i += 1
    return crossing_len, covered_len, seg_len, gaps


def _plane_net_label(net_map, planes) -> Optional[str]:
    if net_map is None:
        return None
    for p in planes:
        n = net_map.net_of(p)
        if n:
            return n
    return None


@register_check("return_path_interruptions")
def run_return_path_interruptions(ctx: CheckContext) -> CheckResult:
    target_max, limit_max = _thresholds(ctx)
    dd = ctx.design_data
    if dd is None:
        return _na(ctx, target_max, limit_max,
                   "No design data; return-path integrity needs net routing + stackup.")

    hs = _high_speed_nets(dd, ctx.check_def.raw or {})
    if not hs:
        return _na(ctx, target_max, limit_max,
                   "No high-speed nets identified (no diff pairs, controlled-impedance, "
                   "or high_speed_net_classes).")

    copper_layers = queries.get_copper_layers(ctx.geometry)
    if len(copper_layers) < 2:
        return _na(ctx, target_max, limit_max,
                   "Fewer than two copper layers; no adjacent reference plane to evaluate.")

    board = queries.get_board_bounds(ctx.geometry)
    board_area = ((board.max_x - board.min_x) * (board.max_y - board.min_y)) if board else 0.0
    plane_min_area = max(20.0, 0.15 * board_area) if board_area > 0 else 20.0

    ordered = sorted(copper_layers, key=lambda L: _rank(_canon_layer(L.logical_layer)))
    canon_of = {id(L): _canon_layer(L.logical_layer) for L in ordered}
    idx_of = {id(L): i for i, L in enumerate(ordered)}

    planes_by_layer: Dict[int, list] = {}
    for L in ordered:
        polys = [p for p in L.polygons
                 if len(p.vertices) >= 3 and _shoelace_area(_poly_pts(p)) >= plane_min_area]
        if polys:
            planes_by_layer[id(L)] = polys

    net_map = get_or_build_net_map(ctx)

    total_cross = 0.0
    evaluated_nets = 0
    violations: List[Violation] = []

    for net_name in sorted(hs):
        net = dd.net(net_name)
        if net is None or not net.has_geometry():
            continue

        segs_by_canon: Dict[Optional[str], List[Tuple[Point, Point]]] = {}
        for (a, b), layer, _w in net.route_segments():
            segs_by_canon.setdefault(_canon_layer(layer), []).append((a, b))

        for scanon, segs in segs_by_canon.items():
            sig_layers = [L for L in ordered if canon_of[id(L)] == scanon]
            if not sig_layers:
                continue
            si = idx_of[id(sig_layers[0])]

            ref_planes: list = []
            for j in (si - 1, si + 1):
                if 0 <= j < len(ordered):
                    ref_planes.extend(planes_by_layer.get(id(ordered[j]), []))
            if not ref_planes:
                continue

            net_cross = 0.0
            covered = 0.0
            total = 0.0
            gap_hits: List[Tuple[float, float, float]] = []
            for a, b in segs:
                c, cov, ln, gaps = _scan_segment(a, b, ref_planes)
                net_cross += c
                covered += cov
                total += ln
                gap_hits.extend(gaps)

            # Only trust nets that are mostly over the plane -- otherwise this
            # isn't really their reference and "gaps" would be spurious.
            if total <= 0.0 or (covered / total) < 0.5:
                continue
            evaluated_nets += 1

            if net_cross > 0.0:
                total_cross += net_cross
                plane_net = _plane_net_label(net_map, ref_planes)
                ref_txt = f"{plane_net} " if plane_net else ""
                for gx, gy, glen in gap_hits:
                    violations.append(Violation(
                        severity=ctx.check_def.severity or "error",
                        message=(
                            f"High-speed net {net_name} crosses a {glen * 1000:.0f} µm gap in the "
                            f"{ref_txt}reference plane on {scanon} (return current has no adjacent path)."
                        ),
                        location=ViolationLocation(
                            net=net_name, layer=scanon, x_mm=gx, y_mm=gy,
                            notes="Return-path interruption: trace over a plane gap/split.",
                        ),
                    ))

    if evaluated_nets == 0:
        return _na(ctx, target_max, limit_max,
                   "No high-speed net was confidently referenced to an adjacent plane "
                   "(insufficient plane coverage under the traces).")

    measured = total_cross
    if measured > limit_max:
        status = "fail"
    elif measured > target_max:
        status = "warning"
    else:
        status = "pass"

    if measured <= target_max:
        score = 100.0
    elif measured >= limit_max:
        score = 0.0
    else:
        span = max(1e-9, limit_max - target_max)
        score = max(0.0, min(100.0, 100.0 * (limit_max - measured) / span))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(measured), target_mm=target_max, limit_high_mm=limit_max,
        ),
        violations=violations,
    ).finalize()
