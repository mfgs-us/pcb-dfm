"""
crosstalk_estimate — first-order coupling risk between sensitive nets.

Crosstalk grows when two *different* high-speed signals run parallel, close, and
for a long distance over a thin dielectric. A full answer needs a field solver;
this is an explicitly **heuristic** geometric estimate built on the net-aware
routing from #1:

    risk% = 100 · coupling · length_weight
    coupling      = 1 / (1 + (S / H)²)         # microstrip coupling falloff
    length_weight = min(1, coupled_len / L_sat)

where ``S`` is the minimum copper edge-to-edge spacing over the region the two
nets run coupled, ``H`` is the dielectric height from the signal layer to its
reference plane (from the stackup; falls back to the trace width when no stackup
is available), and ``L_sat`` is the length past which backward coupling
saturates. The worst risk across all sensitive net *pairs* is reported.

Members of the *same* differential pair are intentionally coupled and are
excluded — crosstalk is between separate signal groups. The check is
``not_applicable`` without design data, without two independent high-speed nets,
or when no sensitive nets actually run coupled.

Metric: estimated coupling percentage (minimize) — target 10%, limit 25%.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.net_map import _canon_layer
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_return_path_interruptions import _high_speed_nets

Point = Tuple[float, float]
# ((start, end), width_mm)
Seg = Tuple[Tuple[Point, Point], float]


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    m = ctx.check_def.metric or {}
    t = m.get("target") or {}
    limits = m.get("limits") or {}
    target = float(t.get("max", 10.0)) if isinstance(t, dict) else 10.0
    limit = float(limits.get("max", 25.0)) if isinstance(limits, dict) else 25.0
    return target, limit


def _na(ctx: CheckContext, target: float, limit: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.ratio_percent(None, target_pct=target, limit_high_pct=limit),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


def _pt_seg(px: float, py: float, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segs_by_layer(net) -> Dict[Optional[str], List[Seg]]:
    out: Dict[Optional[str], List[Seg]] = {}
    for (a, b), layer, width in net.route_segments():
        out.setdefault(_canon_layer(layer), []).append(((a, b), float(width or 0.0)))
    return out


def _couple(a_segs: List[Seg], b_segs: List[Seg], window: float) -> Tuple[float, Optional[float], Optional[Point]]:
    """Coupled length of ``a_segs`` running within ``window`` of ``b_segs``,
    plus the minimum copper edge-to-edge spacing and a representative point."""
    coupled_len = 0.0
    min_edge = math.inf
    at: Optional[Point] = None
    for (a, b), wa in a_segs:
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg_len <= 0.0:
            continue
        n = max(2, min(400, int(seg_len / 0.1) + 1))
        step = seg_len / (n - 1)
        for i in range(n):
            t = i / (n - 1)
            x = a[0] + t * (b[0] - a[0])
            y = a[1] + t * (b[1] - a[1])
            best = math.inf
            best_w = 0.0
            for (c, d), wb in b_segs:
                dist = _pt_seg(x, y, c, d)
                if dist < best:
                    best = dist
                    best_w = wb
            if best <= window:
                coupled_len += step
                edge = max(0.0, best - 0.5 * wa - 0.5 * best_w)
                if edge < min_edge:
                    min_edge = edge
                    at = (x, y)
    return coupled_len, (min_edge if math.isfinite(min_edge) else None), at


@register_check("crosstalk_estimate")
def run_crosstalk_estimate(ctx: CheckContext) -> CheckResult:
    target, limit = _thresholds(ctx)
    dd = ctx.design_data
    if dd is None:
        return _na(ctx, target, limit, "No design data; crosstalk needs net routing geometry.")

    raw = ctx.check_def.raw or {}
    hs = sorted(_high_speed_nets(dd, raw))
    if len(hs) < 2:
        return _na(ctx, target, limit,
                   "Fewer than two high-speed nets; no sensitive pair to evaluate.")

    # Intentionally-coupled diff-pair partners are not crosstalk.
    partners = {frozenset((dp.positive, dp.negative)) for dp in dd.diff_pairs}

    # Dielectric height signal->plane (estimate); falls back to trace width.
    H: Optional[float] = None
    if dd.stackup is not None:
        h = dd.stackup.dielectric_thickness_mm
        if h and h > 0:
            H = float(h)

    l_sat = float(raw.get("saturation_length_mm", 25.0))

    segs_cache: Dict[str, Dict[Optional[str], List[Seg]]] = {}

    def _segs(name: str) -> Dict[Optional[str], List[Seg]]:
        if name not in segs_cache:
            net = dd.net(name)
            segs_cache[name] = _segs_by_layer(net) if net is not None else {}
        return segs_cache[name]

    worst_pct = 0.0
    worst_desc: Optional[Tuple[str, str, float, Optional[Point]]] = None
    evaluated = 0
    violations: List[Violation] = []

    for i in range(len(hs)):
        for j in range(i + 1, len(hs)):
            a_name, b_name = hs[i], hs[j]
            if frozenset((a_name, b_name)) in partners:
                continue
            a_layers = _segs(a_name)
            b_layers = _segs(b_name)
            if not a_layers or not b_layers:
                continue

            for canon, a_segs in a_layers.items():
                b_segs = b_layers.get(canon)
                if not b_segs:
                    continue
                # The pair shares a layer -> it is eligible; if it turns out
                # they never run coupled, that's a real 0% risk (pass), not an
                # inability to evaluate.
                evaluated += 1

                # Reference length for the coupling falloff + capture window.
                widths = [w for _s, w in a_segs] + [w for _s, w in b_segs]
                w_max = max([w for w in widths if w > 0.0], default=0.15)
                ref = H if (H is not None and H > 0.0) else w_max
                window = max(1.0, ref * 5.0)

                coupled_len, s_edge, at = _couple(a_segs, b_segs, window)
                if coupled_len <= 0.0 or s_edge is None:
                    continue  # eligible but uncoupled -> contributes 0% risk

                coupling = 1.0 / (1.0 + (s_edge / ref) ** 2)
                length_weight = min(1.0, coupled_len / l_sat) if l_sat > 0 else 1.0
                risk = 100.0 * coupling * length_weight

                if risk > worst_pct:
                    worst_pct = risk
                    worst_desc = (a_name, b_name, s_edge, at)
                if risk > target:
                    x_mm, y_mm = (at if at is not None else (None, None))
                    violations.append(Violation(
                        severity=ctx.check_def.severity or "info",
                        message=(
                            f"Estimated crosstalk {risk:.0f}% between {a_name} and {b_name} on {canon}: "
                            f"{coupled_len:.1f} mm coupled at {s_edge * 1000:.0f} µm edge spacing "
                            f"(target ≤ {target:.0f}%, limit ≤ {limit:.0f}%). Heuristic estimate."
                        ),
                        location=ViolationLocation(
                            net=f"{a_name}/{b_name}", layer=str(canon),
                            x_mm=x_mm, y_mm=y_mm,
                            notes="Sensitive nets run parallel and close (crosstalk risk).",
                        ),
                    ))

    if evaluated == 0:
        return _na(ctx, target, limit,
                   "No sensitive net pair runs coupled; crosstalk risk not estimated.")

    if worst_pct > limit:
        status = "fail"
    elif worst_pct > target:
        status = "warning"
    else:
        status = "pass"

    if worst_pct <= target:
        score = 100.0
    elif worst_pct >= limit:
        score = 0.0
    else:
        span = max(1e-9, limit - target)
        score = max(0.0, min(100.0, 100.0 * (limit - worst_pct) / span))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.ratio_percent(
            measured_pct=float(worst_pct), target_pct=target, limit_high_pct=limit,
        ),
        violations=violations,
    ).finalize()
