from __future__ import annotations

from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..results import CheckResult, Violation


@register_check("dielectric_thickness_uniformity")
def run_dielectric_thickness_uniformity(ctx: CheckContext) -> CheckResult:
    """
    Placeholder / informational implementation.

    True dielectric thickness uniformity requires stackup and process data:
      - per-layer dielectric thicknesses
      - material properties
      - fabrication process tolerances

    With ONLY a Gerber zip (no IPC-2581 / stackup JSON / fab spec), we cannot
    compute a physically meaningful uniformity metric.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", metric_cfg.get("unit", "%"))

    viol = Violation(
        severity="info",
        message=(
            "Dielectric thickness uniformity cannot be evaluated from Gerbers "
            "alone. Provide stackup / fabrication data (e.g. IPC-2581 or a "
            "separate stackup JSON) to enable this check."
        ),
        location=None,
    )

    # We mark this as a warning-level status but info severity, so it shows up
    # without tanking the overall score.
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status="warning",
        score=80.0,
        metric={
            "kind": "ratio",
            "units": units,
            "measured_value": None,
            "target": None,
            "limit_low": None,
            "limit_high": None,
            "margin_to_limit": None,
        },
        violations=[viol],
    )
