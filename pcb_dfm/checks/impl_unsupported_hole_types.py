from __future__ import annotations

from pathlib import Path
from typing import List

from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..results import CheckResult, Violation
from ..ingest import GerberFileInfo

try:
    import gerber
except Exception:  # pragma: no cover
    gerber = None


_INCH_TO_MM = 25.4


@register_check("unsupported_hole_types")
def run_unsupported_hole_types(ctx: CheckContext) -> CheckResult:
    """
    Heuristic check for hole sizes that are likely outside a typical fab's
    supported range.

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

    if gerber is None or not drill_files:
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
            severity=ctx.check_def.severity,
            status="warning",
            score=80.0,
            metric={
                "kind": "count",
                "units": units,
                "measured_value": None,
                "target": 0.0,
                "limit_low": None,
                "limit_high": float(max_unsupported),
                "margin_to_limit": None,
            },
            violations=[viol],
        )

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
            severity=ctx.check_def.severity,
            status="warning",
            score=80.0,
            metric={
                "kind": "count",
                "units": units,
                "measured_value": None,
                "target": 0.0,
                "limit_low": None,
                "limit_high": float(max_unsupported),
                "margin_to_limit": None,
            },
            violations=[viol],
        )

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
    """
    Extract tool diameters in mm from a drill file using pcb-tools.

    Mirrors the defensive style used in the min_drill_size implementation:
      - gerber.read(path) -> drill_layer
      - drill_layer.to_inch()
      - read tool diameters from drill_layer.tools and from hits[*].tool
    """
    if gerber is None:
        return []

    try:
        drill_layer = gerber.read(str(path))
    except Exception:
        return []

    try:
        drill_layer.to_inch()
    except Exception:
        # assume already inch if conversion not available
        pass

    diam_in: List[float] = []

    tools = getattr(drill_layer, "tools", None)
    if isinstance(tools, dict):
        for tool in tools.values():
            d = getattr(tool, "diameter", None)
            if d is None:
                d = getattr(tool, "size", None)
            if d is not None:
                try:
                    diam_in.append(float(d))
                except Exception:
                    continue

    hits = getattr(drill_layer, "hits", None)
    if hits is not None:
        for hit in hits:
            # new-style hit object
            try:
                tool = getattr(hit, "tool", None)
                if tool is not None and hasattr(tool, "diameter"):
                    d = float(tool.diameter)
                    diam_in.append(d)
                    continue
            except Exception:
                pass

            # older (tool, (x, y)) tuple format
            try:
                tool, _pos = hit
                d = getattr(tool, "diameter", None)
                if d is not None:
                    diam_in.append(float(d))
            except Exception:
                continue

    return [d * _INCH_TO_MM for d in diam_in]
