from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, gerber_aperture_use_bbox_mm
from ..ingest.aperture_validation import validate_apertures
from ..results import CheckResult, Violation, ViolationLocation


@dataclass
class SuspiciousAperture:
    file_label: str
    layer_name: str
    code: str
    shape_norm: str
    dim_mm: Optional[float]
    reason: str
    detail: str


def _aperture_location_mm(path, aperture_code: str, layer_name: str) -> Optional[ViolationLocation]:
    """Pin an aperture-definition finding to somewhere that aperture is used.

    A bad aperture is a *definition*, which has no position of its own, so we
    locate the first object drawn with it. Sourced from the gerbonara backend
    (mm), replacing the pcb-tools primitive walk.
    """
    bbox = gerber_aperture_use_bbox_mm(path, aperture_code)
    if bbox is None:
        return None
    min_x, min_y, max_x, max_y = bbox
    return ViolationLocation(
        layer=layer_name,
        x_mm=0.5 * (min_x + max_x),
        y_mm=0.5 * (min_y + max_y),
        width_mm=max(0.0, max_x - min_x),
        height_mm=max(0.0, max_y - min_y),
        notes="First use of the offending aperture.",
    )
@register_check("aperture_definition_errors")
def run_aperture_definition_errors(ctx: CheckContext) -> CheckResult:
    """
    Detect aperture SIZE outliers and unusable aperture dimensions in Gerber
    artwork.

    Despite the historical check id "aperture_definition_errors", this check
    does NOT detect "missing, ambiguous, or conflicting" aperture *definitions*.
    What it actually flags, per parsed aperture, is:
      - parse_failed:        the Gerber layer could not be parsed at all
      - no_usable_dimension: no numeric size could be extracted for the aperture
      - extremely_small:     size <= min_dim_mm (default 0.01 mm)
      - extremely_large:     size >= max_dim_mm (default 200 mm)
      - macro_no_size / unknown_shape: low-confidence "suspicious" only

    "Hard" reasons (parse/no-dimension/too-small/too-large) drive the metric and
    status; the soft reasons are reported but do not fail the board. In short:
    this is a size/parse sanity check, not a definition-conflict detector.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "count")
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    target_max = float(target_cfg.get("max", 0.0)) if isinstance(target_cfg, dict) else float(target_cfg or 0.0)
    limit_max = float(limits_cfg.get("max", 0.0)) if isinstance(limits_cfg, dict) else float(limits_cfg or 0.0)

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    min_dim_mm = float(raw_cfg.get("min_dim_mm", 0.01))
    max_dim_mm = float(raw_cfg.get("max_dim_mm", 200.0))

    # How many individual violations to emit (beyond the summary)
    max_individual = int(raw_cfg.get("max_individual_violations", 50))
    max_examples = int(raw_cfg.get("max_examples", 12))
    max_files = int(raw_cfg.get("max_files", 999))

    if not GERBONARA_AVAILABLE:
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

    # Detection is delegated to the shared validator (ingest.aperture_validation),
    # which reads gerbonara's typed aperture model. Keeping one implementation
    # means the ingest warnings and this check can never disagree, and drops the
    # near-verbatim duplicate that previously lived here.
    warnings_ = validate_apertures(ctx.ingest.files, max_files=max_files,
                                   max_individual=max_individual)

    suspicious: List[SuspiciousAperture] = [
        SuspiciousAperture(
            file_label=w.file_label,
            layer_name=w.layer_name,
            code=w.code,
            shape_norm=w.shape_norm,
            dim_mm=w.dim_mm,
            reason=w.reason,
            detail=w.detail,
        )
        for w in warnings_
    ]

    per_file_counts: Dict[str, int] = {}
    for s_ in suspicious:
        per_file_counts[s_.layer_name] = per_file_counts.get(s_.layer_name, 0) + 1

    # Map layer label -> source path so a finding can be pinned where the
    # offending aperture is actually used.
    layer_paths: Dict[str, Any] = {
        str(f.logical_layer or f.path.name): f.path
        for f in ctx.ingest.files
        if f.format == "gerber" and f.layer_type in ("copper", "mask", "silk", "silkscreen")
    }

    HARD_REASONS = {
        "parse_failed",
        "no_usable_dimension",
        "extremely_small",
        "extremely_large",
    }

    SOFT_REASONS = {
        "macro_no_size",
        "unknown_shape",
    }

    hard = [s for s in suspicious if s.reason in HARD_REASONS]
    soft = [s for s in suspicious if s.reason in SOFT_REASONS]

    # Metric should reflect real risk, not low-confidence parsing quirks.
    invalid_count = float(len(hard))
    suspicious_count = float(len(soft))
    count = invalid_count  # For backward compatibility

    if invalid_count <= target_max:
        status = "pass"
        sev = ctx.check_def.severity or ctx.check_def.severity_default
        score = 100.0
    elif (limit_max > 0.0) and (invalid_count <= limit_max):
        status = "warning"
        sev = "warning"
        if limit_max > target_max:
            frac = min(1.0, max(0.0, (invalid_count - target_max) / (limit_max - target_max)))
            score = max(60.0, 100.0 - 40.0 * frac)
        else:
            score = 60.0
    else:
        status = "fail"
        sev = "error"
        score = 0.0

    margin_to_limit = None
    if limit_max > 0.0:
        margin_to_limit = float(limit_max - invalid_count)

    violations: List[Violation] = []

    if invalid_count == 0:
        msg = "No invalid aperture definitions detected across Gerber layers."
        if suspicious_count > 0:
            msg += f" Found {int(suspicious_count)} suspicious aperture(s) that do not affect renderability."
        violations.append(
            Violation(
                severity="info",
                message=msg,
                location=None,
            )
        )
    else:
        per_file_sorted = sorted(per_file_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        per_file_summary = {k: v for k, v in per_file_sorted[:10]}

        # Summary violation
        summary = (
            f"Detected {int(invalid_count)} invalid aperture definition(s) across Gerber layers "
            f"(target {target_max:.0f}, limit {limit_max:.0f})"
        )
        if suspicious_count > 0:
            summary += f" plus {int(suspicious_count)} suspicious aperture(s)."
        summary += "."
        violations.append(
            Violation(
                severity=sev,
                message=summary,
                location=None,
                extra={
                    "per_file_counts": per_file_summary,
                    "example_reasons": [f"{s.layer_name} D{s.code} {s.reason}" for s in suspicious[:max_examples]],
                },
            )
        )

        # Individual violations (most useful in UI)
        # Sort by "more serious" reasons first
        priority = {
            "parse_failed": 0,
            "no_apertures_dict": 1,
            "no_usable_dimension": 2,
            "macro_no_size": 3,
            "extremely_small": 4,
            "extremely_large": 5,
            "unknown_shape": 6,
        }

        suspicious_sorted = sorted(suspicious, key=lambda s: (priority.get(s.reason, 99), s.layer_name, s.code))

        for s in suspicious_sorted[:max_individual]:
            msg = (
                f"{s.layer_name}: aperture {s.code} suspicious ({s.reason}). "
                f"shape={s.shape_norm}"
            )
            if s.dim_mm is not None:
                msg += f", size~{s.dim_mm:.3f}mm."
            else:
                msg += "."

            loc = None
            src = layer_paths.get(s.layer_name)
            if src is not None and str(s.code).startswith("D"):
                loc = _aperture_location_mm(src, str(s.code), s.layer_name)

            item_sev = "warning" if s.reason in SOFT_REASONS else sev

            violations.append(
                Violation(
                    severity=item_sev,
                    message=msg,
                    location=loc,
                    extra={
                        "file": s.file_label,
                        "layer": s.layer_name,
                        "aperture_code": s.code,
                        "shape": s.shape_norm,
                        "size_mm": s.dim_mm,
                        "reason": s.reason,
                        "detail": s.detail,
                    },
                )
            )

        if len(suspicious_sorted) > max_individual:
            violations.append(
                Violation(
                    severity="info",
                    message=f"Additional {len(suspicious_sorted) - max_individual} suspicious apertures not listed (cap={max_individual}).",
                    location=None,
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
            "measured_value": invalid_count,
            "target": target_max,
            "limit_low": None,
            "limit_high": limit_max,
            "margin_to_limit": margin_to_limit,
            "invalid_apertures_count": invalid_count,
            "suspicious_apertures_count": suspicious_count,
        },
        violations=violations,
    )
