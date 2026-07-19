from __future__ import annotations

from typing import Optional, List

from pcb_dfm.engine.check_runner import register_check
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.results import CheckResult, MetricResult, Violation  # <- adjust names if needed


@register_check("backdrill_stub_length")
def run_backdrill_stub_length(ctx: CheckContext) -> CheckResult:
    """
    Check that backdrilled vias do not leave excessively long stubs.

    Backdrill depth data is not represented in the current data model:
    BoardGeometry has no notion of backdrilled vias, and the ingest layer
    does not carry per-via drilled-vs-backdrilled depths. Rather than
    fabricate a measurement, this check honestly reports not_applicable.

    Configured limits are still read from ctx.check_def so the check is ready
    to emit a real result once backdrill data becomes available.
    """

    # Limits come from the check definition (limits/metric/raw), never from a
    # nonexistent ctx.rules attribute.
    def _get_rule(name: str, default: float) -> float:
        for source in (ctx.check_def.limits, ctx.check_def.metric, ctx.check_def.raw):
            if isinstance(source, dict) and name in source:
                try:
                    return float(source[name])
                except (TypeError, ValueError):
                    pass
        return default

    # Read (but do not require) the configured stub limit so misconfiguration
    # surfaces here rather than being silently ignored.
    _max_stub_limit_mm: float = _get_rule("max_backdrill_stub_length_mm", default=0.5)

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=100.0,
        metric=None,
        violations=[
            Violation(
                severity="info",
                message=(
                    "Backdrill stub length cannot be evaluated: backdrill depth "
                    "data is not available in the current board model."
                ),
                location=None,
            )
        ],
    )
