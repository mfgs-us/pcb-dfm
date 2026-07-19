from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

from . import GerberFileInfo

try:
    import gerber  # type: ignore
except Exception:
    gerber = None  # type: ignore

_INCH_TO_MM = 25.4


@dataclass(frozen=True)
class ApertureWarning:
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


def _extract_aperture_dim_mm(ap: Any) -> Tuple[Optional[float], Optional[float], str]:
    """Return (min_dim_mm, max_dim_mm, detail).

    The MIN real positive dimension drives the "too small" (minimum feature)
    check so that sliver apertures (e.g. 0.002mm x 4mm) are caught, while the
    MAX dimension drives the "too large" check. Both are None when no usable
    dimension is found.
    """
    dims_inch, notes = _extract_dims_inch(ap)
    if not dims_inch:
        detail = "no numeric dimension found"
        if notes:
            detail += f" ({', '.join(notes)})"
        return None, None, detail

    dims_inch = [d for d in dims_inch if math.isfinite(d) and d > 0.0]
    if not dims_inch:
        detail = "numeric dims present but nonpositive/nonfinite"
        if notes:
            detail += f" ({', '.join(notes)})"
        return None, None, detail

    min_dim_mm = min(dims_inch) * _INCH_TO_MM
    max_dim_mm = max(dims_inch) * _INCH_TO_MM
    detail = f"extracted {len(dims_inch)} dim(s), min={min_dim_mm:.4f}mm, max={max_dim_mm:.4f}mm"
    if notes:
        detail += f" ({', '.join(notes)})"
    return min_dim_mm, max_dim_mm, detail


def validate_apertures(
    files: List[GerberFileInfo],
    *,
    min_dim_mm: float = 0.01,
    max_dim_mm: float = 200.0,
    max_files: int = 200,
    max_individual: int = 500,
) -> List[ApertureWarning]:
    if gerber is None:
        return [
            ApertureWarning(
                file_label="(all)",
                layer_name="(all)",
                code="(import)",
                shape_norm="unknown",
                dim_mm=None,
                reason="gerber_lib_missing",
                detail="gerber parsing library missing, cannot validate apertures",
            )
        ]

    suspicious: List[ApertureWarning] = []

    gerber_files = [
        f for f in files
        if f.format == "gerber" and f.layer_type in ("copper", "mask", "silk", "silkscreen")
    ]

    for k, info in enumerate(gerber_files):
        if k >= max_files or len(suspicious) >= max_individual:
            break

        layer_label = str(info.logical_layer or info.path.name)
        file_label = str(info.path.name)

        unit_ok = True
        try:
            layer = gerber.read(str(info.path))
            try:
                layer.to_inch()
            except Exception:
                # Unit conversion failed: dims are NOT guaranteed to be in
                # inches. Since _extract_aperture_dim_mm multiplies by 25.4
                # assuming inches, a mm-native aperture would be inflated ~25x
                # and produce a bogus "too_large". Flag as indeterminate
                # instead of running the numeric size comparisons.
                unit_ok = False
        except Exception:
            suspicious.append(
                ApertureWarning(
                    file_label=file_label,
                    layer_name=layer_label,
                    code="(parse)",
                    shape_norm="unknown",
                    dim_mm=None,
                    reason="parse_failed",
                    detail="failed to parse Gerber",
                )
            )
            continue

        apertures = getattr(layer, "apertures", None)
        if not apertures:
            continue

        if isinstance(apertures, dict):
            ap_items = list(apertures.items())
        else:
            items_fn = getattr(apertures, "items", None)
            if callable(items_fn):
                ap_items = list(items_fn())
            else:
                ap_items = [(f"(idx:{i})", ap) for i, ap in enumerate(list(apertures))]

        for code, ap in ap_items:
            if len(suspicious) >= max_individual:
                break

            shape_norm = _normalize_shape(ap)
            min_dim_mm_val, max_dim_mm_val, dim_detail = _extract_aperture_dim_mm(ap)

            if shape_norm == "macro" and min_dim_mm_val is None:
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=None,
                        reason="macro_no_size",
                        detail=dim_detail,
                    )
                )
                continue

            if min_dim_mm_val is None or max_dim_mm_val is None:
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=None,
                        reason="no_usable_dimension",
                        detail=f"shape={shape_norm}, {dim_detail}",
                    )
                )
                continue

            if not unit_ok:
                # Unit conversion failed; the dimensions cannot be trusted in
                # mm-space, so we cannot reliably compare against thresholds.
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=None,
                        reason="unit_indeterminate",
                        detail=f"unit conversion failed, size indeterminate ({dim_detail})",
                    )
                )
                continue

            # Minimum-feature check uses the SMALLEST real dimension so that
            # thin slivers (e.g. 0.002mm x 4mm) are caught.
            if min_dim_mm_val < min_dim_mm:
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=min_dim_mm_val,
                        reason="too_small",
                        detail=f"{min_dim_mm_val:.6f}mm < min {min_dim_mm:.6f}mm ({dim_detail})",
                    )
                )
                continue

            # Oversized check uses the LARGEST dimension.
            if max_dim_mm_val > max_dim_mm:
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=max_dim_mm_val,
                        reason="too_large",
                        detail=f"{max_dim_mm_val:.3f}mm > max {max_dim_mm:.3f}mm ({dim_detail})",
                    )
                )
                continue

    return suspicious
