from __future__ import annotations

from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, excellon_tool_diameters_mm
from ..ingest import GerberFileInfo
from ..results import CheckResult, MetricResult, Violation

_INCH_TO_MM = 25.4
_DEFAULT_BOARD_THICKNESS_MM = 1.6


def _resolve_board_thickness_mm(ctx: CheckContext) -> float:
    """Board thickness for the aspect-ratio denominator.

    Prefers the design-data stackup (sum of all layer thicknesses) when one is
    supplied; falls back to a 1.6 mm default when no usable stackup is present.
    """
    design_data = getattr(ctx, "design_data", None)
    stackup = getattr(design_data, "stackup", None) if design_data else None
    if stackup is not None:
        try:
            total = stackup.total_thickness_mm()
        except Exception:
            total = None
        if total is not None and total > 0:
            return float(total)
    return _DEFAULT_BOARD_THICKNESS_MM


@register_check("drill_aspect_ratio")
def run_drill_aspect_ratio(ctx: CheckContext) -> CheckResult:
    """
    Compute drill aspect ratio:

        aspect_ratio = board_thickness_mm / min_drill_diameter_mm

    Limits are typically expressed as max allowed aspect ratio.

    Board thickness is taken from the design-data stackup when one is supplied
    (the sum of every stackup layer thickness), and falls back to 1.6 mm only
    when no stackup is available.

    Limitation: the aspect ratio always uses the *full* board thickness. Blind
    and buried vias only span part of the stack, so their true aspect ratio is
    lower than reported here -- those holes are over-estimated (conservative),
    which is an accepted limitation.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", metric_cfg.get("unit", ""))

    limits = ctx.check_def.limits or {}
    recommended_max = float(limits.get("recommended_max", 8.0))
    absolute_max = float(limits.get("absolute_max", 10.0))

    # Board thickness: prefer the design-data stackup (sum of layer
    # thicknesses); fall back to 1.6 mm only when no stackup is supplied.
    board_thickness_mm = _resolve_board_thickness_mm(ctx)

    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "drill"
    ]

    if not GERBONARA_AVAILABLE or not drill_files:
        viol = Violation(
            severity="warning",
            message="No drill parser available or no drill files found to compute drill aspect ratio.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=50.0,
            metric=MetricResult.dimensionless(
                measured=None,
                target=recommended_max,
                limit_high=absolute_max,
            ),
            violations=[viol],
        ).finalize()

    diameters_mm: List[float] = []
    for info in drill_files:
        diameters_mm.extend(_extract_tool_diameters_mm(info.path))

    if not diameters_mm:
        viol = Violation(
            severity="warning",
            message="No drill tools or hits found to compute drill aspect ratio.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=50.0,
            metric=MetricResult.dimensionless(
                measured=None,
                target=recommended_max,
                limit_high=absolute_max,
            ),
            violations=[viol],
        ).finalize()

    min_d_mm = min(diameters_mm)
    aspect = board_thickness_mm / min_d_mm

    # Decide status only (severity handled by finalize)
    if aspect > absolute_max:
        status = "fail"
    elif aspect > recommended_max:
        status = "warning"
    else:
        status = "pass"

    # Score: 100 at <= recommended_max, 0 at >= absolute_max
    if aspect <= recommended_max:
        score = 100.0
    elif aspect >= absolute_max:
        score = 0.0
    else:
        span = absolute_max - recommended_max
        score = max(0.0, min(100.0, 100.0 * (absolute_max - aspect) / span))

    margin_to_limit = float(absolute_max - aspect)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Drill aspect ratio {aspect:.2f}:1 exceeds recommended {recommended_max:.2f}:1 "
            f"(absolute maximum {absolute_max:.2f}:1)."
        )
        violations.append(
            Violation(
                severity=ctx.check_def.severity,
                message=msg,
                location=None,
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity="info",  # Default value, will be overridden by finalize()
        status=status,
        score=score,
        metric=MetricResult.dimensionless(
            measured=float(aspect),
            target=recommended_max,
            limit_high=absolute_max,
        ),
        violations=violations,
    ).finalize()


def _extract_tool_diameters_mm(path) -> List[float]:
    """Drill tool diameters in mm, via the gerbonara parse backend (#3).

    The pcb-tools path read tool diameters in the file's native unit and
    multiplied by 25.4 unconditionally, so mm-native drill files reported
    diameters 25.4x too large.
    """
    return excellon_tool_diameters_mm(path)
