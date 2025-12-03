from __future__ import annotations

from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..results import CheckResult, Violation


@register_check("impedance_control")
def run_impedance_control(ctx: CheckContext) -> CheckResult:
    """
    Informational implementation for impedance control.

    A real impedance check needs:
      - stackup (dielectric thickness, Er, copper thickness)
      - trace geometry per net (width, spacing, reference plane)
      - which nets are supposed to be controlled

    None of that is reliably available from bare Gerbers, so for now we emit
    an informational violation explaining what is missing.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", metric_cfg.get("unit", "ohm"))

    viol = Violation(
        severity="info",
        message=(
            "Impedance control cannot be validated from Gerber artwork alone. "
            "Provide stackup + net constraints (e.g. IPC-2581, ODB++, or a "
            "separate constraint file) to enable impedance checking."
        ),
        location=None,
    )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status="warning",
        score=80.0,
        metric={
            "kind": "geometry",
            "units": units,
            "measured_value": None,
            "target": None,
            "limit_low": None,
            "limit_high": None,
            "margin_to_limit": None,
        },
        violations=[viol],
    )
