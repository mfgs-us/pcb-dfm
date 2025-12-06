# pcb_dfm/checks/impl_backdrill_stub_length.py

from __future__ import annotations

from typing import Optional

from pcb_dfm.engine.check_runner import register_check
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.results import CheckResult


@register_check("backdrill_stub_length")
def run_backdrill_stub_length(ctx: CheckContext) -> CheckResult:
    """
    Check that backdrilled vias do not leave excessively long stubs.

    Assumptions / expected geometry model (adapt these to your real types):

    - ctx.geometry.backdrilled_vias: iterable of via-like objects
    - each via has:
        - total_depth_mm: total via barrel depth in mm
        - backdrilled_depth_mm: how much of that barrel has been removed by backdrilling

      So the residual stub is:

          stub_mm = max(total_depth_mm - backdrilled_depth_mm, 0)

    - ctx.rules (or ctx.check_def.ruleset) exposes a limit such as:
        max_backdrill_stub_length_mm

      If not found, we fall back to a conservative default.
    """

    geom = ctx.geometry

    # 1) If the geometry has no notion of backdrills, treat as not applicable.
    if not hasattr(geom, "backdrilled_vias"):
        return ctx.make_not_applicable_result(
            message="No backdrilled via metadata available; skipping backdrill stub length check."
        )

    backdrilled = getattr(geom, "backdrilled_vias", None)
    if not backdrilled:
        return ctx.make_not_applicable_result(
            message="Board has no backdrilled vias; backdrill stub length check not applicable."
        )

    # 2) Pull limit from rules, with a sane default.
    #    Adjust how you read this to match your real rules model.
    def _get_rule(name: str, default: float) -> float:
        rules = getattr(ctx, "rules", None)
        if rules is not None and hasattr(rules, name):
            try:
                value = float(getattr(rules, name))
                return value
            except (TypeError, ValueError):
                pass
        return default

    max_stub_limit_mm: float = _get_rule(
        "max_backdrill_stub_length_mm",
        default=0.5,  # default limit; tune to your fab guideline
    )

    total_backdrilled = 0
    violating_count = 0
    worst_stub_mm: float = 0.0

    # 3) Scan all backdrilled vias and compute stubs.
    for via in backdrilled:
        total_backdrilled += 1

        total_depth: Optional[float] = getattr(via, "total_depth_mm", None)
        backdrilled_depth: Optional[float] = getattr(via, "backdrilled_depth_mm", None)

        if total_depth is None or backdrilled_depth is None:
            # If metadata is incomplete, just skip this via but keep going.
            continue

        stub_mm = max(total_depth - backdrilled_depth, 0.0)
        if stub_mm > worst_stub_mm:
            worst_stub_mm = stub_mm

        if stub_mm > max_stub_limit_mm:
            violating_count += 1

    if total_backdrilled == 0:
        return ctx.make_not_applicable_result(
            message="No usable backdrilled via depth data; skipping backdrill stub length check."
        )

    # 4) Build metric object.
    #    Replace ctx.build_metric(...) with whatever helper your other impl_* checks use.
    metric = ctx.build_metric(
        kind="max_backdrill_stub_length",
        units="mm",
        measured_value=worst_stub_mm,
        target=None,
        limit_low=None,
        limit_high=max_stub_limit_mm,
    )

    # 5) Derive status / severity / score.
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
        severity = "warning"
        # Simple linear score: you can replace with your own scoring function.
        score = max(
            0.0,
            100.0 * (1.0 - violating_count / max(total_backdrilled, 1)),
        )
        message = (
            f"{violating_count}/{total_backdrilled} backdrilled vias exceed stub length "
            f"limit of {max_stub_limit_mm:.3f} mm (worst {worst_stub_mm:.3f} mm)."
        )

    # 6) Construct CheckResult.
    #    Replace ctx.make_result(...) with whatever pattern your other checks use
    #    (e.g. a CheckResult constructor or helper).
    return ctx.make_result(
        status=status,
        severity=severity,
        message=message,
        metric=metric,
    )
