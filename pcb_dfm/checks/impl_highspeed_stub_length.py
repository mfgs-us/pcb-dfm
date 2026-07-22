"""
highspeed_stub_length — dangling branches on high-speed nets.

An unterminated branch (a tee that dead-ends) on a high-speed net reflects the
signal; the longer the stub, the worse. Built on the routing topology from #11:
each high-speed net's routed copper is turned into a graph and its longest
planar stub is measured (see ``geometry.net_topology``). The worst stub across
all high-speed nets is reported.

Via-transition dead ends are excluded (that Z-axis stub is
``backdrill_stub_length``). This complements the *planar* case.

Heuristic: without per-pin data we can't prove a given branch is an unintended
stub rather than a routed load, but any long unterminated branch on a high-speed
net is a reflection concern regardless. ``not_applicable`` without design data,
high-speed nets, or measurable routed geometry. Metric: max stub length (mm),
target 5 / limit 10 (minimize).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.net_topology import max_stub_length_mm
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_return_path_interruptions import _high_speed_nets


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    m = ctx.check_def.metric or {}
    t = m.get("target") or {}
    limits = m.get("limits") or {}
    target = float(t.get("max", 5.0)) if isinstance(t, dict) else 5.0
    limit = float(limits.get("max", 10.0)) if isinstance(limits, dict) else 10.0
    return target, limit


def _na(ctx: CheckContext, target: float, limit: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.geometry_mm(None, target_mm=target, limit_high_mm=limit),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


@register_check("highspeed_stub_length")
def run_highspeed_stub_length(ctx: CheckContext) -> CheckResult:
    target, limit = _thresholds(ctx)
    dd = ctx.design_data
    if dd is None:
        return _na(ctx, target, limit, "No design data; stub length needs per-net routing.")

    hs = sorted(_high_speed_nets(dd, ctx.check_def.raw or {}))
    if not hs:
        return _na(ctx, target, limit,
                   "No high-speed nets identified (no diff pairs, controlled-impedance, "
                   "or high_speed_net_classes).")

    worst = 0.0
    worst_net: Optional[str] = None
    evaluated = 0
    per_net: List[Tuple[str, float]] = []
    for name in hs:
        net = dd.net(name)
        if net is None or not net.has_geometry():
            continue
        stub = max_stub_length_mm(net)
        if stub is None:
            continue
        evaluated += 1
        per_net.append((name, stub))
        if stub > worst:
            worst = stub
            worst_net = name

    if evaluated == 0:
        return _na(ctx, target, limit,
                   "No high-speed net had measurable routed geometry to analyse for stubs.")

    if worst > limit:
        status = "fail"
    elif worst > target:
        status = "warning"
    else:
        status = "pass"

    if worst <= target:
        score = 100.0
    elif worst >= limit:
        score = 0.0
    else:
        span = max(1e-9, limit - target)
        score = max(0.0, min(100.0, 100.0 * (limit - worst) / span))

    violations: List[Violation] = []
    if status != "pass":
        for name, stub in sorted(per_net, key=lambda t: -t[1]):
            if stub <= target:
                break
            violations.append(Violation(
                severity=ctx.check_def.severity or "warning",
                message=(
                    f"High-speed net {name} has a {stub:.1f} mm stub "
                    f"(target ≤ {target:.0f} mm, limit ≤ {limit:.0f} mm). "
                    f"Heuristic — unterminated branch length."
                ),
                location=ViolationLocation(net=name, notes="Dangling branch on a high-speed net."),
            ))

    _ = worst_net
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(worst), target_mm=target, limit_high_mm=limit,
        ),
        violations=violations,
    ).finalize()
