from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..results import CheckResult, Violation
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo

try:
    import gerber  # type: ignore
except Exception:
    gerber = None  # type: ignore

_INCH_TO_MM = 25.4


@dataclass
class SuspiciousAperture:
    file_label: str
    layer_name: str
    code: str
    shape_norm: str
    dim_mm: Optional[float]
    reason: str
    detail: str


def _safe_float(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except Exception:
        return None
    if not math.isfinite(x):
        return None
    return x


def _iter_numeric_fields(obj: Any) -> Iterable[Tuple[str, Any]]:
    direct = [
        "width",
        "height",
        "x_size",
        "y_size",
        "diameter",
        "radius",
        "outer_diameter",
        "inner_diameter",
        "hole_diameter",
        "hole",
        "drill",
    ]
    for k in direct:
        if hasattr(obj, k):
            yield k, getattr(obj, k)

    for k in ("modifiers", "parameters", "param", "params"):
        if hasattr(obj, k):
            yield k, getattr(obj, k)

    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        for k, v in d.items():
            lk = str(k).lower()
            if any(t in lk for t in ("width", "height", "size", "diam", "radius", "hole", "drill", "outer", "inner")):
                yield str(k), v


def _extract_dims_inch(ap: Any) -> Tuple[List[float], List[str]]:
    dims: List[float] = []
    notes: List[str] = []

    for name, val in _iter_numeric_fields(ap):
        if val is None:
            continue

        lname = name.lower()

        if lname == "radius":
            fv = _safe_float(val)
            if fv is None:
                continue
            if fv <= 0.0:
                notes.append("radius<=0")
                continue
            dims.append(fv * 2.0)
            continue

        if isinstance(val, (list, tuple)):
            for item in val:
                fv = _safe_float(item)
                if fv is None or fv <= 0.0:
                    continue
                dims.append(fv)
            continue

        if isinstance(val, dict):
            for item in val.values():
                fv = _safe_float(item)
                if fv is None or fv <= 0.0:
                    continue
                dims.append(fv)
            continue

        fv = _safe_float(val)
        if fv is None:
            continue
        if fv <= 0.0:
            notes.append(f"{lname}<=0")
            continue
        dims.append(fv)

    return dims, notes


def _normalize_shape(ap: Any) -> str:
    shape = getattr(ap, "shape", None)

    if isinstance(shape, str):
        s = shape.strip().lower()
        if s in ("c", "circle"):
            return "circle"
        if s in ("r", "rect", "rectangle"):
            return "rectangle"
        if s in ("o", "oval", "obround"):
            return "obround"
        if s in ("p", "poly", "polygon"):
            return "polygon"
        if s in ("am", "macro"):
            return "macro"
        if s:
            if s.startswith("am") or "macro" in s:
                return "macro"
            return s

    for k in ("macro", "macro_name", "aperture_macro"):
        v = getattr(ap, k, None)
        if isinstance(v, str) and v.strip():
            return "macro"

    if hasattr(ap, "primitives"):
        return "macro"

    return "unknown"


def _extract_aperture_dim_mm(ap: Any) -> Tuple[Optional[float], str]:
    dims_inch, notes = _extract_dims_inch(ap)
    if not dims_inch:
        detail = "no numeric dimension found"
        if notes:
            detail += f" ({', '.join(notes)})"
        return None, detail

    # filter non-finite/non-positive
    dims_inch = [d for d in dims_inch if math.isfinite(d) and d > 0.0]
    if not dims_inch:
        detail = "numeric dims present but nonpositive/nonfinite"
        if notes:
            detail += f" ({', '.join(notes)})"
        return None, detail

    max_dim_mm = max(dims_inch) * _INCH_TO_MM
    detail = f"extracted {len(dims_inch)} dim(s), max={max_dim_mm:.4f}mm"
    if notes:
        detail += f" ({', '.join(notes)})"
    return max_dim_mm, detail


@register_check("aperture_definition_errors")
def run_aperture_definition_errors(ctx: CheckContext) -> CheckResult:
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

    suspicious: List[SuspiciousAperture] = []
    per_file_counts: Dict[str, int] = {}

    gerber_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files
        if f.format == "gerber" and f.layer_type in ("copper", "mask", "silk", "silkscreen")
    ]

    for k, info in enumerate(gerber_files):
        if k >= max_files:
            break
        layer_label = str(info.logical_layer or info.path.name)
        file_label = str(info.path.name)

        try:
            layer = gerber.read(str(info.path))
        except Exception:
            suspicious.append(
                SuspiciousAperture(
                    file_label=file_label,
                    layer_name=layer_label,
                    code="(parse)",
                    shape_norm="unknown",
                    dim_mm=None,
                    reason="parse_failed",
                    detail="failed to parse Gerber",
                )
            )
            per_file_counts[layer_label] = per_file_counts.get(layer_label, 0) + 1
            continue

        try:
            layer.to_inch()
        except Exception:
            pass

        apertures = getattr(layer, "apertures", None)
        if not apertures:
            continue

        # pcb-tools sometimes returns apertures as a dict, or a dict_values/list-like view.
        # Normalize to an iterable of (code, aperture) pairs.
        ap_items: Iterable[Tuple[Any, Any]]
        if isinstance(apertures, dict):
            ap_items = apertures.items()
        else:
            # Try .items() if it exists
            items_fn = getattr(apertures, "items", None)
            if callable(items_fn):
                ap_items = items_fn()
            else:
                # Fallback: treat it as a sequence of aperture objects (no codes available)
                ap_items = [(f"(idx:{i})", ap) for i, ap in enumerate(list(apertures))]

        for code, ap in ap_items:
            if len(suspicious) >= max_individual:
                break
            shape_norm = _normalize_shape(ap)
            dim_mm_val, dim_detail = _extract_aperture_dim_mm(ap)

            # Macro apertures are common. Only suspicious if no numeric size can be extracted.
            if shape_norm == "macro" and dim_mm_val is None:
                suspicious.append(
                    SuspiciousAperture(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=None,
                        reason="macro_no_size",
                        detail=dim_detail,
                    )
                )
                per_file_counts[layer_label] = per_file_counts.get(layer_label, 0) + 1
                continue

            # No size extracted at all
            if dim_mm_val is None:
                suspicious.append(
                    SuspiciousAperture(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=None,
                        reason="no_usable_dimension",
                        detail=f"shape={shape_norm}, {dim_detail}",
                    )
                )
                per_file_counts[layer_label] = per_file_counts.get(layer_label, 0) + 1
                continue

            # Tiny/huge
            if dim_mm_val <= min_dim_mm:
                suspicious.append(
                    SuspiciousAperture(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=dim_mm_val,
                        reason="extremely_small",
                        detail=f"size={dim_mm_val:.4f}mm <= {min_dim_mm:.4f}mm ({dim_detail})",
                    )
                )
                per_file_counts[layer_label] = per_file_counts.get(layer_label, 0) + 1
            elif dim_mm_val >= max_dim_mm:
                suspicious.append(
                    SuspiciousAperture(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=dim_mm_val,
                        reason="extremely_large",
                        detail=f"size={dim_mm_val:.2f}mm >= {max_dim_mm:.2f}mm ({dim_detail})",
                    )
                )
                per_file_counts[layer_label] = per_file_counts.get(layer_label, 0) + 1

            # Unknown shape with otherwise sane size: low-confidence suspicious
            if shape_norm == "unknown":
                suspicious.append(
                    SuspiciousAperture(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=dim_mm_val,
                        reason="unknown_shape",
                        detail=f"unknown shape but size={dim_mm_val:.3f}mm ({dim_detail})",
                    )
                )
                per_file_counts[layer_label] = per_file_counts.get(layer_label, 0) + 1

    count = float(len(suspicious))

    if count <= target_max:
        status = "pass"
        sev = ctx.check_def.severity or ctx.check_def.severity_default
        score = 100.0
    elif (limit_max > 0.0) and (count <= limit_max):
        status = "warning"
        sev = "warning"
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
        per_file_sorted = sorted(per_file_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        per_file_summary = {k: v for k, v in per_file_sorted[:10]}

        # Summary violation
        summary = (
            f"Detected {int(count)} suspicious aperture definition(s) across Gerber layers "
            f"(target {target_max:.0f}, limit {limit_max:.0f})."
        )
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

            violations.append(
                Violation(
                    severity=sev,
                    message=msg,
                    location=None,
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
            "measured_value": count,
            "target": target_max,
            "limit_low": None,
            "limit_high": limit_max,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
