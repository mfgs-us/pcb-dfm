from __future__ import annotations

from typing import List, Tuple

from ..results import CheckResult, Violation
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo

try:
    import gerber
except Exception:
    gerber = None

_INCH_TO_MM = 25.4


def _extract_aperture_dims(ap) -> Tuple[float, bool]:
    """
    Best effort extraction of a characteristic aperture size in mm.

    Returns (max_dim_mm, ok_flag).
    ok_flag=False means we could not get anything sane.
    """
    dims_inch: List[float] = []

    for attr in ("width", "height", "x_size", "y_size", "diameter", "radius"):
        val = getattr(ap, attr, None)
        if val is None:
            continue
        try:
            v = float(val)
        except Exception:
            continue
        # radius -> diameter
        if attr == "radius":
            v = v * 2.0
        if v > 0.0:
            dims_inch.append(v)

    if not dims_inch:
        return 0.0, False

    max_dim_mm = max(dims_inch) * _INCH_TO_MM
    return max_dim_mm, True


@register_check("aperture_definition_errors")
def run_aperture_definition_errors(ctx: CheckContext) -> CheckResult:
    """
    Scan Gerber layers for obviously invalid or suspicious aperture definitions.

    Heuristics:
      - Parse all Gerber format files via pcb-tools.
      - For each aperture:
          - If no usable dimension can be extracted -> suspicious.
          - If max dimension <= min_dim_mm or >= max_dim_mm -> suspicious.
          - If aperture shape is missing or unknown -> suspicious.
    Metric:
      - measured_value: total number of suspicious apertures found.
      - target: 0
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "count")
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    target_max = float(target_cfg.get("max", 0.0)) if isinstance(target_cfg, dict) else float(target_cfg or 0.0)
    limit_max = float(limits_cfg.get("max", 0.0)) if isinstance(limits_cfg, dict) else float(limits_cfg or 0.0)

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    min_dim_mm = float(raw_cfg.get("min_dim_mm", 0.01))   # <= 10 µm is probably bogus in aperture def
    max_dim_mm = float(raw_cfg.get("max_dim_mm", 200.0))  # > 200 mm is clearly bogus

    suspicious: List[str] = []

    if gerber is None:
        viol = Violation(
            severity="warning",
            message="Gerber parser not available; cannot inspect aperture definitions.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity or ctx.check_def.severity_default,
            status="warning",
            score=50.0,
            metric={
                "kind": "count",
                "units": units,
                "measured_value": None,
                "target": target_max,
                "limit_low": None,
                "limit_high": limit_max,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Consider all Gerber graphics layers
    gerber_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files
        if f.format == "gerber"
    ]

    for info in gerber_files:
        try:
            layer = gerber.read(str(info.path))
        except Exception:
            suspicious.append(f"{info.logical_layer or info.path.name}: failed to parse Gerber for aperture check")
            continue

        try:
            layer.to_inch()
        except Exception:
            pass

        apertures = getattr(layer, "apertures", None)
        if not isinstance(apertures, dict) or not apertures:
            # If there are primitives but no apertures, that is at least unusual.
            prims = getattr(layer, "primitives", [])
            if prims:
                suspicious.append(f"{info.logical_layer or info.path.name}: primitives present but no apertures defined")
            continue

        for code, ap in apertures.items():
            # Aperture shape
            shape = getattr(ap, "shape", None)
            shape_ok = shape in ("circle", "rectangle", "rect", "oval", "polygon", "donut")

            dim_mm, ok_dim = _extract_aperture_dims(ap)

            # no usable dimension at all
            if not ok_dim:
                suspicious.append(
                    f"{info.logical_layer or info.path.name}: aperture {code} has no usable dimension"
                )
                continue

            # tiny or huge apertures are suspicious
            if dim_mm <= min_dim_mm:
                suspicious.append(
                    f"{info.logical_layer or info.path.name}: aperture {code} has extremely small size {dim_mm:.4f} mm"
                )
            elif dim_mm >= max_dim_mm:
                suspicious.append(
                    f"{info.logical_layer or info.path.name}: aperture {code} has extremely large size {dim_mm:.2f} mm"
                )

            if not shape_ok:
                suspicious.append(
                    f"{info.logical_layer or info.path.name}: aperture {code} has unknown/unspecified shape {shape!r}"
                )

    count = float(len(suspicious))

    # Status logic
    if count <= target_max:
        status = "pass"
        sev = ctx.check_def.severity or ctx.check_def.severity_default
        score = 100.0
    elif count <= limit_max if limit_max > 0 else False:
        status = "warning"
        sev = "warning"
        # crude linear drop from 100 -> 60 across [target_max, limit_max]
        if limit_max > target_max:
            frac = min(1.0, max(0.0, (count - target_max) / (limit_max - target_max)))
            score = max(60.0, 100.0 - 40.0 * frac)
        else:
            score = 60.0
    else:
        status = "fail"
        sev = "error"
        score = 0.0

    margin_to_limit = None
    if limit_max > 0.0:
        margin_to_limit = float(limit_max - count)

    violations: List[Violation] = []
    if count == 0:
        violations.append(
            Violation(
                severity="info",
                message="No suspicious aperture definitions detected across Gerber layers.",
                location=None,
            )
        )
    else:
        # Summarize, with details in extra
        summary = (
            f"Detected {int(count)} suspicious aperture definition(s) in Gerber layers "
            f"(target {target_max:.0f}, limit {limit_max:.0f})."
        )
        violations.append(
            Violation(
                severity=sev,
                message=summary,
                location=None,
                extra={"examples": suspicious[:10]},  # cap for readability
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity or ctx.check_def.severity_default,
        status=status,
        score=score,
        metric={
            "kind": "count",
            "units": units,
            "measured_value": count,
            "target": target_max,
            "limit_low": None,
            "limit_high": limit_max,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
