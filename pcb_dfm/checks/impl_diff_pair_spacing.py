from __future__ import annotations

import math

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation, ViolationLocation


def _nested_max(metric_cfg: dict, key: str, default: float) -> float:
    node = metric_cfg.get(key)
    if isinstance(node, dict) and isinstance(node.get("max"), (int, float)):
        return float(node["max"])
    if isinstance(node, (int, float)):
        return float(node)
    return default


def _pt_seg_dist(p, a, b) -> float:
    """Euclidean distance from point p to segment a-b (mm)."""
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _same_layer(l1, l2) -> bool:
    return l1 is None or l2 is None or l1 == l2


@register_check("diff_pair_spacing")
def run_diff_pair_spacing(ctx: CheckContext) -> CheckResult:
    """
    Differential-pair spacing consistency.

    Verifies that the edge-to-edge gap between the two members of a diff pair is
    held constant along their coupled length. Because a constant trace width
    only shifts the gap by a constant, the *variation* metric is independent of
    width, so we report the range (max - min) of the centre-to-centre gap in
    microns. Small = well-controlled coupling; large = the gap wanders.

    Needs per-net routed geometry (segments), available from an IPC-2581 import
    or a sidecar that provides net ``segments``. Otherwise not_applicable.
    """
    metric_cfg = ctx.check_def.metric or {}
    raw_cfg = ctx.check_def.raw or {}
    target_um = _nested_max(metric_cfg, "target", 25.0)
    limit_um = _nested_max(metric_cfg, "limits", 50.0)
    coupling_max_mm = float(raw_cfg.get("coupling_max_mm", 2.0))

    dd = ctx.design_data
    pairs = dd.diff_pairs if dd is not None else []

    worst_um = 0.0
    worst_pair = None
    evaluated = 0
    violations = []

    for pair in pairs:
        pos = dd.net(pair.positive) if dd is not None else None
        neg = dd.net(pair.negative) if dd is not None else None
        if pos is None or neg is None:
            continue
        p_segs = pos.route_segments()
        n_segs = neg.route_segments()
        if not p_segs or not n_segs:
            continue

        # Sample each positive segment (endpoints + midpoint) and record the
        # centre-to-centre gap to the nearest coupled negative segment.
        gaps = []
        for (a, b), p_layer, _w in p_segs:
            for pt in (a, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), b):
                best = None
                for (na, nb), n_layer, _nw in n_segs:
                    if not _same_layer(p_layer, n_layer):
                        continue
                    d = _pt_seg_dist(pt, na, nb)
                    if best is None or d < best:
                        best = d
                if best is not None and best <= coupling_max_mm:
                    gaps.append(best)

        if len(gaps) < 2:
            continue  # not enough coupled geometry to judge consistency
        evaluated += 1
        variation_um = (max(gaps) - min(gaps)) * 1000.0
        if variation_um > worst_um:
            worst_um = variation_um
            worst_pair = pair
        if variation_um > target_um:
            sev = "error" if variation_um > limit_um else ctx.check_def.severity
            violations.append(Violation(
                message=(
                    f"Diff pair {pair.name}: intra-pair gap varies by "
                    f"{variation_um:.1f} um along the coupled length "
                    f"(target <= {target_um:.0f} um, absolute <= {limit_um:.0f} um)."
                ),
                severity=sev,
                location=ViolationLocation(net=pair.name),
            ))

    if evaluated == 0:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",
            score=None,
            metric=MetricResult(kind="distance", units="um", measured_value=None),
            violations=[Violation(
                message=(
                    "Differential-pair spacing cannot be evaluated without net "
                    "routing geometry. Provide an IPC-2581 import (or a sidecar "
                    "with net 'segments') that includes the pair's traces."
                ),
                severity="info",
            )],
        )

    if worst_um > limit_um:
        status = "fail"
    elif worst_um > target_um:
        status = "warning"
    else:
        status = "pass"

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=None,
        metric=MetricResult(
            kind="distance", units="um",
            measured_value=worst_um,
            target=target_um,
            limit_high=limit_um,
            margin_to_limit=limit_um - worst_um,
        ),
        violations=violations,
    ).finalize()
