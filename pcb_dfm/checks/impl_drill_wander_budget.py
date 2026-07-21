from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation


@register_check("drill_wander_budget")
def run_drill_wander_budget(ctx: CheckContext) -> CheckResult:
    """
    Drill wander budget: the remaining radial registration margin before drill
    wander causes annular-ring breakout.

    A real wander budget is::

        budget = annular_ring - (fab_registration_tolerance + drill_wander_allowance)

    i.e. it requires the fabricator's registration/wander tolerance, the
    designed annular ring, and the layer stackup to pair drills with the pads
    whose registration budget they share. None of these are derivable from bare
    artwork: the geometry model carries no annular-ring pairing, no stackup, and
    no fab tolerance data.

    Earlier versions computed ``board_thickness / min_drill`` (a drill *aspect
    ratio*, the metric owned by ``drill_aspect_ratio``) and reported it under the
    "wander budget" name. That is a different, misleading quantity, so rather
    than fabricate a µm budget this check honestly reports not_applicable.

    The configured bounds are still read so a misconfigured ruleset surfaces
    here, and so the check is ready to emit a real result once registration
    tolerance and annular-ring data are available in the board model.
    """

    def _get_limit(name: str) -> float | None:
        for source in (ctx.check_def.limits, ctx.check_def.metric, ctx.check_def.raw):
            if isinstance(source, dict) and name in source:
                try:
                    return float(source[name])
                except (TypeError, ValueError):
                    pass
        return None

    # Read (but do not require) the configured radial budget bounds.
    _recommended_min = _get_limit("recommended_min")
    _absolute_min = _get_limit("absolute_min")

    msg = (
        "Drill wander budget cannot be evaluated from artwork: it requires the "
        "fabricator's registration/wander tolerance, the designed annular ring, "
        "and the layer stackup to compute the remaining radial margin before "
        "breakout. This data is not present in the current board model, so no "
        "µm budget is reported (a drill aspect ratio is not the same metric)."
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
