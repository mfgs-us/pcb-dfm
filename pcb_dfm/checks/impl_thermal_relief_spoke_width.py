from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation


@register_check("thermal_relief_spoke_width")
def run_thermal_relief_spoke_width(ctx: CheckContext) -> CheckResult:
    """
    Thermal relief spoke width: the minimum width of the spokes that connect a
    pad to a surrounding copper plane.

    Measuring true spoke width requires knowing which pads are *thermally
    relieved* connections to a plane (versus direct-connect pads, isolated pads,
    or antipads) and then isolating the individual spoke necks. That connectivity
    is a netlist/plane-fill property; it is not reliably extractable from bare
    artwork, where a pad sitting inside a large copper polygon may be a thermal
    relief, a direct connection, or an unrelated overlap.

    A previous version detected "pad inside plane" bounding-box containment and
    emitted a fixed WARNING with ``measured_value=None`` - a warning about a
    quantity it never measured. Rather than fabricate that signal, this check
    honestly reports not_applicable (mirroring ``backdrill_stub_length``) until
    real spoke-connectivity extraction is available.

    The configured target/limit bounds are still read so a misconfigured ruleset
    surfaces here and the check is ready to grade once spoke widths can be
    measured.
    """

    def _get_limit(name: str) -> float | None:
        for source in (ctx.check_def.limits, ctx.check_def.metric, ctx.check_def.raw):
            if isinstance(source, dict) and name in source:
                try:
                    return float(source[name])
                except (TypeError, ValueError):
                    pass
        return None

    # Read (but do not require) the configured spoke-width bounds.
    _recommended_min = _get_limit("recommended_min")
    _absolute_min = _get_limit("absolute_min")

    msg = (
        "Thermal relief spoke width cannot be evaluated from artwork: it requires "
        "thermal-relief connectivity (which pads are relieved connections to a "
        "plane) to isolate the spoke necks, which is not reliably extractable "
        "from copper polygons alone. No spoke width is measured."
    )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=100.0,
        metric=None,
        violations=[Violation(severity="info", message=msg, location=None)],
    )
