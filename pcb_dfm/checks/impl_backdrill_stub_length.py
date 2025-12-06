from __future__ import annotations

from typing import Optional, List

from pcb_dfm.engine.check_runner import register_check
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.results import CheckResult, MetricResult, Violation  # <- adjust names if needed


@register_check("backdrill_stub_length")
def run_backdrill_stub_length(ctx: CheckContext) -> CheckResult:
    """
    Check that backdrilled vias do not leave excessively long stubs.

    Assumed geometry model (adapt to your actual fields):

    - ctx.geometry.backdrilled_vias: iterable of via-like objects
    - each via has:
        - total_depth_mm: total via barrel depth in mm
        - backdrilled_depth_mm: how much of that barrel is removed

      residual stub:
          stub_mm = max(total_depth_mm - backdrilled_depth_mm, 0)
    """

    geom = ctx.geometry

    # 1) No geometry support -> "not_applicable"
    if not hasattr(geom, "backdrilled_vias"):
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            status="not_applicable",
            severity="info",
            score=100.0,
            metric=None,
            violations=[
                Violation(
                    severity="info",
                    message="No backdrilled via metadata available; skipping backdrill stub length check.",
                    location=None,
                )
            ],
        )

    backdrilled = getattr(geom, "backdrilled_vias", None)
    if not backdrilled:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            status="not_applicable",
            severity="info",
            score=100.0,
            metric=None,
            violations=[
                Violation(
                    severity="info",
                    message="Board has no backdrilled vias; backdrill stub length check not applicable.",
                    location=None,
                )
            ],
        )

    # 2) Get limit from rules, or fallback default
    def _get_rule(name: str, default: float) -> float:
        rules = getattr(ctx, "rules", None)
        if rules is not None and hasattr(rules, name):
            try:
                return float(getattr(rules, name))
            except (TypeError, ValueError):
                pass
        return default

    max_stub_limit_mm: float = _get_rule(
        "max_backdrill_stub_length_mm",
        default=0.5,  # tune this
    )

    total_backdrilled = 0
    violating_count = 0
    worst_stub_mm: float = 0.0

    for via in backdrilled:
        total_backdrilled += 1

        total_depth: Optional[float] = getattr(via, "total_depth_mm", None)
        backdrilled_depth: Optional[float] = getattr(via, "backdrilled_depth_mm", None)

        if total_depth is None or backdrilled_depth is None:
            # skip incomplete data
            continue

        stub_mm = max(total_depth - backdrilled_depth, 0.0)
        if stub_mm > worst_stub_mm:
            worst_stub_mm = stub_mm

        if stub_mm > max_stub_limit_mm:
            violating_count += 1

    if total_backdrilled == 0:
        # we had a container but no usable data
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            status="not_applicable",
            severity="info",
            score=100.0,
            metric=None,
            violations=[
                Violation(
                    severity="info",
                    message="No usable backdrilled via depth data; skipping backdrill stub length check.",
                    location=None,
                )
            ],
        )

    # 3) Build metric
    metric = MetricResult(
        kind="max_backdrill_stub_length",
        units="mm",
        measured_value=worst_stub_mm,
        target=None,
        limit_low=None,
        limit_high=max_stub_limit_mm,
    )

    # 4) Status / severity / score
    if violating_count == 0:
        status = "pass"
        severity = "info"
        score = 100.0
        message = (
            f"All {total_backdrilled} backdrilled vias have stub length "
            f"<= {max_stub_limit_mm:.3f} mm (worst {worst_stub_mm:.3f} mm)."
        )
    else:
        status = "warning"
        severity = "error"  # match your other checks' pattern
        # simple linear scoring
        score = max(
            0.0,
            100.0 * (1.0 - violating_count / max(total_backdrilled, 1)),
        )
        message = (
            f"{violating_count}/{total_backdrilled} backdrilled vias exceed stub length "
            f"limit of {max_stub_limit_mm:.3f} mm (worst {worst_stub_mm:.3f} mm)."
        )

    violations: List[Violation] = []
    if violating_count > 0:
        violations.append(
            Violation(
                severity="error",
                message=message,
                location=None,  # you can add a specific via location later
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        status=status,
        severity=severity,
        score=score,
        metric=metric,
        violations=violations,
    )
