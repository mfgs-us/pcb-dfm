from __future__ import annotations

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


@register_check("diff_pair_skew")
def run_diff_pair_skew(ctx: CheckContext) -> CheckResult:
    """
    Differential-pair length skew.

    Needs connectivity (which nets form a pair and their routed lengths), which
    bare Gerbers do not carry. When design_data provides diff pairs and per-net
    routed length (e.g. from an IPC-2581 import or the JSON sidecar), we compute
    the intra-pair length skew and compare against the configured target/limit.
    Otherwise not_applicable.
    """
    metric_cfg = ctx.check_def.metric or {}
    target_mm = _nested_max(metric_cfg, "target", 0.5)
    limit_mm = _nested_max(metric_cfg, "limits", 1.0)

    dd = ctx.design_data
    pairs = dd.diff_pairs if dd is not None else []

    worst_skew = 0.0
    evaluated = 0
    violations = []

    for pair in pairs:
        pos = dd.net(pair.positive) if dd is not None else None
        neg = dd.net(pair.negative) if dd is not None else None
        if pos is None or neg is None:
            continue
        lp, ln = pos.routed_length_mm(), neg.routed_length_mm()
        if lp <= 0.0 or ln <= 0.0:
            continue  # no routed length known for this pair
        evaluated += 1
        skew = abs(lp - ln)
        worst_skew = max(worst_skew, skew)
        if skew > target_mm:
            sev = "error" if skew > limit_mm else ctx.check_def.severity
            violations.append(Violation(
                message=(
                    f"Diff pair {pair.name}: length skew {skew:.3f} mm "
                    f"({pair.positive}={lp:.3f} mm, {pair.negative}={ln:.3f} mm; "
                    f"target <= {target_mm:.3f} mm, absolute <= {limit_mm:.3f} mm)."
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
            metric=MetricResult.geometry_mm(measured_mm=None),
            violations=[Violation(
                message=(
                    "Differential-pair skew cannot be evaluated from Gerbers "
                    "alone. Provide design data (IPC-2581 or a sidecar) with "
                    "diff pairs and per-net routed lengths to enable it."
                ),
                severity="info",
            )],
        )

    if worst_skew > limit_mm:
        status = "fail"
    elif worst_skew > target_mm:
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
        metric=MetricResult.geometry_mm(
            measured_mm=worst_skew,
            target_mm=target_mm,
            limit_high_mm=limit_mm,
        ),
        violations=violations,
    ).finalize()
