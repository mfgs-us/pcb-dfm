from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation


def _nested_max(metric_cfg: dict, key: str, default: float) -> float:
    node = metric_cfg.get(key)
    if isinstance(node, dict) and isinstance(node.get("max"), (int, float)):
        return float(node["max"])
    if isinstance(node, (int, float)):
        return float(node)
    return default


@register_check("dielectric_thickness_uniformity")
def run_dielectric_thickness_uniformity(ctx: CheckContext) -> CheckResult:
    """
    Dielectric thickness uniformity.

    Requires per-layer dielectric thicknesses, which bare Gerbers do not carry.
    When a design-data sidecar supplies ``stackup.dielectric_layers_mm`` we
    compute the maximum deviation from the mean thickness (in microns) and
    compare against the configured target/limit. Otherwise not_applicable.
    """
    metric_cfg = ctx.check_def.metric or {}
    target_um = _nested_max(metric_cfg, "target", 10.0)
    limit_um = _nested_max(metric_cfg, "limits", 25.0)

    dd = ctx.design_data
    stackup = dd.stackup if dd is not None else None
    thicknesses = stackup.dielectric_thicknesses_mm() if stackup is not None else []

    usable = [float(x) for x in thicknesses if x > 0]

    if len(usable) < 2:
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
                    "Dielectric thickness uniformity cannot be evaluated from "
                    "Gerbers alone. Provide a design-data sidecar with "
                    "'stackup.dielectric_layers_mm' (>= 2 layers) to enable it."
                ),
                severity="info",
            )],
        )

    mean = sum(usable) / len(usable)
    max_dev_um = max(abs(x - mean) for x in usable) * 1000.0  # mm -> um

    if max_dev_um > limit_um:
        status = "fail"
    elif max_dev_um > target_um:
        status = "warning"
    else:
        status = "pass"

    violations = []
    if status != "pass":
        violations.append(Violation(
            message=(
                f"Dielectric thickness deviates up to {max_dev_um:.1f} um from "
                f"the {mean * 1000.0:.1f} um mean (target <= {target_um:.0f} um, "
                f"absolute <= {limit_um:.0f} um)."
            ),
            severity=ctx.check_def.severity,
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=None,
        metric=MetricResult(
            kind="distance",
            units="um",
            measured_value=max_dev_um,
            target=target_um,
            limit_high=limit_um,
            margin_to_limit=limit_um - max_dev_um,
        ),
        violations=violations,
    ).finalize()
