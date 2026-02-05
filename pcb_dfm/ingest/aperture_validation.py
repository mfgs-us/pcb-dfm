from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

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


def _extract_aperture_dim_mm(ap: Any) -> Tuple[Optional[float], str]:
    dims_inch, notes = _extract_dims_inch(ap)
    if not dims_inch:
        detail = "no numeric dimension found"
        if notes:
            detail += f" ({', '.join(notes)})"
        return None, detail

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

        try:
            layer = gerber.read(str(info.path))
            try:
                layer.to_inch()
            except Exception:
                pass
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
            ap_items = apertures.items()
        else:
            items_fn = getattr(apertures, "items", None)
            if callable(items_fn):
                ap_items = items_fn()
            else:
                ap_items = [(f"(idx:{i})", ap) for i, ap in enumerate(list(apertures))]

        for code, ap in ap_items:
            if len(suspicious) >= max_individual:
                break

            shape_norm = _normalize_shape(ap)
            dim_mm_val, dim_detail = _extract_aperture_dim_mm(ap)

            if shape_norm == "macro" and dim_mm_val is None:
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

            if dim_mm_val is None:
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

            if dim_mm_val < min_dim_mm:
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=dim_mm_val,
                        reason="too_small",
                        detail=f"{dim_mm_val:.6f}mm < min {min_dim_mm:.6f}mm ({dim_detail})",
                    )
                )
                continue

            if dim_mm_val > max_dim_mm:
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label,
                        layer_name=layer_label,
                        code=str(code),
                        shape_norm=shape_norm,
                        dim_mm=dim_mm_val,
                        reason="too_large",
                        detail=f"{dim_mm_val:.3f}mm > max {max_dim_mm:.3f}mm ({dim_detail})",
                    )
                )
                continue

    return suspicious
