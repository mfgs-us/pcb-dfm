from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..geometry.gerber_backend import GERBONARA_AVAILABLE, gerber_apertures_mm
from . import GerberFileInfo


@dataclass(frozen=True)
class ApertureWarning:
    file_label: str
    layer_name: str
    code: str
    shape_norm: str
    dim_mm: Optional[float]
    reason: str
    detail: str


def validate_apertures(
    files: List[GerberFileInfo],
    *,
    min_dim_mm: float = 0.01,
    max_dim_mm: float = 200.0,
    max_files: int = 200,
    max_individual: int = 500,
) -> List[ApertureWarning]:
    """Flag aperture definitions that look wrong.

    Reads gerbonara's *typed* aperture model via the parse backend (#3), so
    shapes and dimensions come from the parser rather than sniffing numeric
    attributes off an untyped object, and units are converted explicitly (an
    aperture keeps the file's native unit). That also removes the old
    "unit_indeterminate" state, which only existed because the previous path
    multiplied by 25.4 assuming inches.
    """
    if not GERBONARA_AVAILABLE:
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

        apertures = gerber_apertures_mm(info.path)
        if apertures is None:
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

        for ap in apertures:
            if len(suspicious) >= max_individual:
                break

            # A macro with no derivable size is reported distinctly: we cannot
            # size-check it, but its presence is not itself an error.
            if ap.min_dim_mm is None and ap.shape == "macro":
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label, layer_name=layer_label, code=ap.code,
                        shape_norm=ap.shape, dim_mm=None,
                        reason="macro_no_size", detail=ap.detail,
                    )
                )
                continue

            # No positive dimension at all -- e.g. a zero-size aperture, which
            # is invalid per the Gerber spec and still gets used to draw with.
            if ap.min_dim_mm is None or ap.max_dim_mm is None:
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label, layer_name=layer_label, code=ap.code,
                        shape_norm=ap.shape, dim_mm=None,
                        reason="no_usable_dimension",
                        detail=f"shape={ap.shape}, {ap.detail}",
                    )
                )
                continue

            # Minimum-feature check uses the SMALLEST real dimension so thin
            # slivers (e.g. 0.002mm x 4mm) are caught.
            if ap.min_dim_mm < min_dim_mm:
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label, layer_name=layer_label, code=ap.code,
                        shape_norm=ap.shape, dim_mm=ap.min_dim_mm,
                        reason="too_small",
                        detail=f"{ap.min_dim_mm:.6f}mm < min {min_dim_mm:.6f}mm ({ap.detail})",
                    )
                )
                continue

            # Oversized check uses the LARGEST dimension.
            if ap.max_dim_mm > max_dim_mm:
                suspicious.append(
                    ApertureWarning(
                        file_label=file_label, layer_name=layer_label, code=ap.code,
                        shape_norm=ap.shape, dim_mm=ap.max_dim_mm,
                        reason="too_large",
                        detail=f"{ap.max_dim_mm:.3f}mm > max {max_dim_mm:.3f}mm ({ap.detail})",
                    )
                )
                continue

    return suspicious
