from __future__ import annotations

from pathlib import Path
from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, excellon_hits_mm
from ..ingest import GerberFileInfo
from ..results import CheckResult, MetricResult, Violation

_INCH_TO_MM = 25.4


@register_check("unsupported_hole_types")
def run_unsupported_hole_types(ctx: CheckContext) -> CheckResult:
    """
    Check for hole SIZES (drill diameters) that are likely outside a typical
    fab's supported drill range.

    IMPORTANT: despite the historical check id "unsupported_hole_types", this
    check only inspects drilled hole *diameters*. It does NOT (and cannot from
    flat Excellon/Gerber drill artwork) detect blind, buried, or other special
    hole *types* -- that classification requires per-layer span / stackup data
    which is not available here. The name/description were corrected to say
    "hole sizes".

    We DO NOT know the specific fab capabilities; instead we:
      - read all drill files via pcb-tools
      - extract all tool diameters in mm
      - count diameters < min_diameter_mm or > max_diameter_mm

    The thresholds come from check_def.limits:
      - min_diameter_mm (default 0.2 mm)
      - max_diameter_mm (default 6.0 mm)
      - max_unsupported (default 2)
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", metric_cfg.get("unit", "count"))

    limits = ctx.check_def.limits or {}
    min_supported = float(limits.get("min_diameter_mm", 0.2))
    max_supported = float(limits.get("max_diameter_mm", 6.0))
    max_unsupported = int(limits.get("max_unsupported", 2))

    # Collect drill files
    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "drill"
    ]

    if not GERBONARA_AVAILABLE or not drill_files:
        msg = (
            "No drill parser available or no drill files found; "
            "cannot classify unsupported hole types."
        )
        viol = Violation(
            severity="info",
            message=msg,
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=80.0,
            metric=MetricResult(
                kind="count",
                units=units,
                measured_value=None,
                target=0.0,
                limit_low=None,
                limit_high=float(max_unsupported),
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    diameters_mm: List[float] = []
    for info in drill_files:
        diameters_mm.extend(_extract_diameters_mm(info.path))

    if not diameters_mm:
        msg = "No drill tools or hits found; cannot classify unsupported hole types."
        viol = Violation(
            severity="info",
            message=msg,
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=80.0,
            metric=MetricResult(
                kind="count",
                units=units,
                measured_value=None,
                target=0.0,
                limit_low=None,
                limit_high=float(max_unsupported),
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    unsupported: List[float] = [
        d for d in diameters_mm if d < min_supported or d > max_supported
    ]
    count_unsupported = len(unsupported)

    # Decide status based on count vs allowed
    if count_unsupported == 0:
        status = "pass"
        severity = ctx.check_def.severity or "error"
        score = 100.0
    elif count_unsupported <= max_unsupported:
        status = "warning"
        severity = "warning"
        # simple linear drop: one unsupported -> ~70, at max_unsupported -> 50
        score = max(0.0, min(100.0, 100.0 - 30.0 * count_unsupported))
    else:
        status = "fail"
        severity = "error"
        score = 0.0

    margin_to_limit = float(max_unsupported - count_unsupported)

    violations: List[Violation] = []
    if count_unsupported > 0:
        msg = (
            f"Detected {count_unsupported} hole size(s) outside the nominal "
            f"supported range [{min_supported:.3f} mm, {max_supported:.3f} mm]. "
            "Confirm against your fab's drill capability table."
        )
        examples = sorted(set(f"{d:.3f} mm" for d in unsupported))[:10]
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=None,
                extra={"example_diameters_mm": examples},
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status=status,
        score=score,
        metric={
            "kind": "count",
            "units": units,
            "measured_value": float(count_unsupported),
            "target": 0.0,
            "limit_low": None,
            "limit_high": float(max_unsupported),
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )


def _extract_diameters_mm(path: Path) -> List[float]:
    """Drill diameters in mm, via the gerbonara parse backend (#3)."""
    return [h.diameter_mm for h in excellon_hits_mm(path)]
