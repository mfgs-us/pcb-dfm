# pcb_dfm/geometry/gerber_parser.py

from __future__ import annotations

import logging
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..ingest import GerberIngestResult
from .gerber_backend import GERBONARA_AVAILABLE, gerber_polygons_mm
from .layer_model import BoardGeometry, BoardLayer
from .primitives import Point2D, Polygon

# Gerber layer polygons are extracted via gerbonara (see gerber_backend, #3):
# proper arcs/regions/apertures, mm-native.


# Copper polygons below this area (mm^2) are degenerate pour-boundary
# artifacts, not real copper. See _populate_layer_polygons_with_gerber.
_MIN_COPPER_POLY_AREA_MM2 = 1e-4


@dataclass
class GerberFormatInfo:
    units: str  # "inch" or "mm"
    int_digits: int
    dec_digits: int


def build_board_geometry(ingest: GerberIngestResult) -> BoardGeometry:
    """
    Build a board geometry model from a GerberIngestResult.

    - Organizes files into logical BoardLayer objects.
    - Extracts polygons for all Gerber-based layers (copper, mask,
      silkscreen, outline, mechanical) via gerbonara.
    - If gerbonara is unavailable, or if outline parsing yields nothing,
      falls back to a naive outline-only parser for outline layers.

    This gives real geometric primitives in mm-space for later DFM checks.
    """
    geom = BoardGeometry(root_dir=ingest.root_dir)

    # Key by (logical_layer, side, layer_type)
    layer_map: Dict[Tuple[str, str, str], BoardLayer] = {}

    for f in ingest.files:
        key = (f.logical_layer, f.side, f.layer_type)
        if key not in layer_map:
            name = f.logical_layer
            layer = BoardLayer(
                name=name,
                logical_layer=f.logical_layer,
                side=f.side,
                layer_type=f.layer_type,
            )
            layer_map[key] = layer
            geom.add_layer(layer)

        layer = layer_map[key]
        layer.file_ids.append(f.id)
        layer.files.append(f)

    # Populate polygons via gerbonara (see gerber_backend).
    if GERBONARA_AVAILABLE:
        for layer in geom.layers:
            _populate_layer_polygons_with_gerber(layer)
    else:
        # gerbonara is a declared hard dependency. If its import failed we cannot
        # extract copper/mask/silk geometry at all, so every geometry-based check
        # would pass vacuously. Make this degradation loud rather than silently
        # producing an empty (but "clean") board. The outline fallback below still
        # runs so basic outline checks can proceed.
        msg = (
            "gerbonara failed to import: copper/mask/silkscreen polygons will "
            "NOT be extracted and geometry-based DFM checks will pass vacuously. "
            "Only the naive outline fallback is available."
        )
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        logging.getLogger("pcb_dfm.geometry").warning("%s", msg)

    # Fallback: ensure outline has at least one polygon
    for layer in geom.layers:
        if layer.layer_type == "outline" and not layer.polygons:
            _populate_outline_polygons_fallback(layer)

    return geom


# ------------------------------
# gerbonara-based polygon extraction
# ------------------------------


def _populate_layer_polygons_with_gerber(layer: BoardLayer) -> None:
    """
    Populate polygons on a BoardLayer using the gerbonara backend, which
    returns mm-space polygons directly (arcs tessellated, regions filled).

    We do this for:
    - copper
    - mask
    - silkscreen
    - outline
    - mechanical
    and ignore drill layers here.
    """
    if not GERBONARA_AVAILABLE:
        return

    if layer.layer_type == "drill":
        return  # handled separately when we care about drills

    for f in layer.files:
        if f.format != "gerber":
            continue

        polys = gerber_polygons_mm(f.path)
        # Copper pours are often drawn as many zero-/near-zero-width boundary
        # lines that render to degenerate (≈ zero-area) polygons. They carry no
        # copper and pollute spacing/annular/clearance checks (spurious ~0). Drop
        # them here — the floor is far below any real feature (a 10 µm square is
        # 1e-4 mm²), and well below copper_sliver's own 0.02 mm² floor, so real
        # thin slivers survive. Outline strokes are intentionally thin (their
        # *path* is the edge), so they are never filtered.
        if layer.layer_type == "copper":
            polys = [p for p in polys if _poly_area_mm2(p) >= _MIN_COPPER_POLY_AREA_MM2]
        layer.polygons.extend(polys)


def _poly_area_mm2(poly: Polygon) -> float:
    v = poly.vertices
    n = len(v)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        j = (i + 1) % n
        s += v[i].x * v[j].y - v[j].x * v[i].y
    return abs(s) * 0.5


def _populate_outline_polygons_fallback(layer: BoardLayer) -> None:
    """
    4C) Improved fallback outline polygon extraction with selective parsing.

    Only fallback parse if the file name strongly indicates outline,
    and only take coordinates from draw commands (D01) to avoid non-outline moves.
    """
    for f in layer.files:
        # Only use fallback for files that strongly indicate outline content
        if not _is_strong_outline_candidate(f.path, f.original_name):
            continue

        polys = _extract_outline_polygons_from_file_fallback(f.path)
        layer.polygons.extend(polys)


def _is_strong_outline_candidate(path: Path, original_name: str) -> bool:
    """
    Check if file is a strong outline candidate for fallback parsing.

    This prevents parsing random X/Y coordinates from non-outline Gerbers.
    """
    name_lower = original_name.lower()
    ext = path.suffix.lower()

    # Strong outline indicators
    strong_indicators = [
        "edge_cuts", "edgecuts", "outline", "boardoutline",
        "board_edge", "board-edge", "profile", "contour"
    ]

    # Check for strong indicators in name
    if any(indicator in name_lower for indicator in strong_indicators):
        return True

    # Check for outline-specific extensions
    if ext in {".gko", ".gm1", ".gml"}:
        return True

    # If it's a generic .gbr, be more cautious
    if ext == ".gbr":
        # Only proceed if there's at least one strong indicator
        return any(indicator in name_lower for indicator in strong_indicators)

    return False


def _extract_outline_polygons_from_file_fallback(path: Path) -> List[Polygon]:
    """
    Naive outline polygon extractor (used when pcb-tools is unavailable or the
    primary path yields nothing).

    Handles the parts of RS-274X coordinate handling that matter for outlines:

    - Modal coordinates: a line may specify only X or only Y; the unspecified
      axis carries forward from the current point.
    - Pen-up moves (D02) separate contours. Each D02 starts a NEW contour and
      the move-to point becomes that contour's first vertex. This keeps a
      board-with-cutout as multiple polygons instead of one garbage polygon.
    - Draw moves (D01) append their endpoint to the current contour.
    - Flashes (D03) are ignored for outlines.
    - Arcs (G02/G03) are approximated by a straight segment to their endpoint.
      This is a known limitation: curved edges become chords. It avoids
      silently dropping the arc (which would corrupt the outline), but does not
      reconstruct the true arc geometry (I/J center offsets are not used).

    One Polygon is emitted per contour that has >= 3 points.
    """
    text = path.read_text(errors="ignore")

    fmt = _detect_gerber_format(text)
    scale = _format_to_scale(fmt)

    # Parse X, Y, and the operation D-code independently so we correctly handle
    # lines that start with a G-code (e.g. "G02X..Y..D01") or specify only one
    # axis (modal moves like "Y10000D01").
    x_re = re.compile(r"X(-?\d+)", re.IGNORECASE)
    y_re = re.compile(r"Y(-?\d+)", re.IGNORECASE)
    d_re = re.compile(r"D0?([123])\b", re.IGNORECASE)

    contours: List[List[Point2D]] = []
    current: List[Point2D] = []

    cur_x: Optional[int] = None
    cur_y: Optional[int] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        upper = line.upper()

        # Skip parameter / macro / comment / control lines outright.
        if any(cmd in upper for cmd in ["AD", "AM", "SR", "G04", "M02", "M00", "%"]):
            continue

        d_match = d_re.search(line)
        if not d_match:
            continue
        d_tok = d_match.group(1)

        x_match = x_re.search(line)
        y_match = y_re.search(line)

        # Modal coordinates: carry forward the axis not specified on this line.
        if x_match is not None:
            cur_x = int(x_match.group(1))
        if y_match is not None:
            cur_y = int(y_match.group(1))

        # Need a fully defined current point before we can emit a vertex.
        if cur_x is None or cur_y is None:
            continue

        pt = Point2D(x=cur_x * scale, y=cur_y * scale)

        if d_tok == "2":
            # Pen-up move: close out the previous contour and start a new one,
            # seeding it with the move-to point.
            if len(current) >= 3:
                contours.append(current)
            current = [pt]
        elif d_tok in ("1",):
            # Pen-down draw (or arc, approximated as a straight segment to the
            # endpoint): append endpoint to the current contour.
            current.append(pt)
        # d_tok == "3" (flash) is ignored for outlines.

    if len(current) >= 3:
        contours.append(current)

    polys: List[Polygon] = []
    for pts in contours:
        # Close the contour if not already closed.
        first = pts[0]
        last = pts[-1]
        if first.x != last.x or first.y != last.y:
            pts = pts + [Point2D(x=first.x, y=first.y)]
        if len(pts) >= 3:
            polys.append(Polygon(vertices=pts))

    return polys


def _detect_gerber_format(text: str) -> GerberFormatInfo:
    units = "inch"
    int_digits = 2
    dec_digits = 5

    if "%MOMM" in text.upper():
        units = "mm"
    elif "%MOIN" in text.upper():
        units = "inch"

    fs_match = re.search(r"%FS[^X]*X(\d)(\d)Y(\d)(\d)\*%", text.upper())
    if fs_match:
        try:
            int_digits = int(fs_match.group(1))
            dec_digits = int(fs_match.group(2))
        except ValueError:
            pass

    return GerberFormatInfo(units=units, int_digits=int_digits, dec_digits=dec_digits)


def _format_to_scale(fmt: GerberFormatInfo) -> float:
    if fmt.units == "mm":
        unit_scale = 1.0
    else:
        unit_scale = 25.4

    coord_scale = 10.0 ** (-fmt.dec_digits)
    return unit_scale * coord_scale
