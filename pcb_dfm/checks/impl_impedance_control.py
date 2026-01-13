from __future__ import annotations

from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..results import CheckResult, Violation, MetricResult


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
    viol = Violation(
        message=(
            "Impedance control cannot be validated from Gerber artwork alone. "
            "Provide stackup + net constraints (IPC-2581, ODB++, or constraint file)."
        ),
        severity="info",
        location=None,
        extra={},
    )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="warning",        # or "not_applicable"
        severity="info",         # placeholder, finalize should normalize
        score=80.0,              # optional
        metric=MetricResult(
            kind="ratio",
            units="%",
            measured_value=None,
            target=8.0,
            limit_high=10.0
        ),
        violations=[viol],
    ).finalize()
