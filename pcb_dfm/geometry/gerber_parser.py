# pcb_dfm/geometry/gerber_parser.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple, List, Optional
import math
import re
import builtins

from ..ingest import GerberIngestResult
from .layer_model import BoardLayer, BoardGeometry
from .primitives import Point2D, Polygon

# ---------------------------------
# Fix for pcb-tools using "rU" mode
# ---------------------------------
_builtin_open = open  # type: ignore[assignment]


def fixed_open(filename, mode="r", *args, **kwargs):
    if mode == "rU":
        mode = "r"
    return _builtin_open(filename, mode, *args, **kwargs)


builtins.open = fixed_open

# ---------------------------------
# Try to use pcb-tools (gerber)
# ---------------------------------
try:
    import gerber
    from gerber.primitives import Circle, Rectangle, Line  # type: ignore
except Exception:
    gerber = None
    Circle = Rectangle = Line = object  # type: ignore


@dataclass
class GerberFormatInfo:
    units: str  # "inch" or "mm"
    int_digits: int
    dec_digits: int


def build_board_geometry(ingest: GerberIngestResult) -> BoardGeometry:
    """
    Build a board geometry model from a GerberIngestResult.

    - Organizes files into logical BoardLayer objects.
    - If pcb-tools (gerber lib) is available:
        - extracts polygons for all Gerber-based layers (copper, mask,
          silkscreen, outline, mechanical).
    - If pcb-tools is not available, or if outline parsing yields nothing:
        - falls back to a naive outline-only parser for outline layers.

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

    # Populate polygons with pcb-tools if available
    if gerber is not None:
        for layer in geom.layers:
            _populate_layer_polygons_with_gerber(layer)

    # Fallback: ensure outline has at least one polygon
    for layer in geom.layers:
        if layer.layer_type == "outline" and not layer.polygons:
            _populate_outline_polygons_fallback(layer)

    return geom


# ------------------------------
# pcb-tools based polygon extraction
# ------------------------------


def _populate_layer_polygons_with_gerber(layer: BoardLayer) -> None:
    """
    Populate polygons on a BoardLayer using pcb-tools (gerber) primitives.

    We:
    - parse each Gerber file backing this layer
    - call .to_inch() to normalize
    - convert inches -> mm
    - turn primitives into our Polygon objects

    We do this for:
    - copper
    - mask
    - silkscreen
    - outline
    - mechanical
    and ignore drill layers here.
    """
    if gerber is None:
        return

    if layer.layer_type == "drill":
        return  # handled separately when we care about drills

    for f in layer.files:
        if f.format != "gerber":
            continue

        polys = _extract_polygons_from_gerber_file(f.path)
        layer.polygons.extend(polys)


def _extract_polygons_from_gerber_file(path: Path) -> List[Polygon]:
    """
    Use pcb-tools to read a Gerber file and convert primitives into
    approximate filled polygons in mm.

    Strategy:
    - layer.primitives is iterated
    - if prim has vertices -> direct polygon
    - Circle -> approximated as N-gon
    - Rectangle -> 4-point polygon
    - Line -> rectangular strip around the segment based on width

    Coordinates in pcb-tools layers are in whatever unit the file uses;
    we convert to inch via .to_inch() and then to mm.
    """
    try:
        layer = gerber.read(str(path))  # type: ignore[arg-type]
    except Exception:
        # If parsing fails, do not crash the engine; just skip this file.
        return []

    # Normalize to inch, then convert to mm
    try:
        layer.to_inch()
    except Exception:
        # If to_inch is not available or fails, we bail on this file
        return []

    polys: List[Polygon] = []

    for prim in getattr(layer, "primitives", []):
        # 1) true polygon / region
        if getattr(prim, "vertices", None):
            try:
                pts = [Point2D(x=_inch_to_mm(x), y=_inch_to_mm(y)) for (x, y) in prim.vertices]
                if len(pts) >= 3:
                    polys.append(Polygon(vertices=pts))
            except Exception:
                continue
            continue

        # 2) flashed round pad
        if isinstance(prim, Circle) and getattr(prim, "flashed", False):
            try:
                cx, cy = prim.position
                r = prim.radius
                circle_poly = _circle_to_polygon_mm(cx, cy, r)
                polys.append(circle_poly)
            except Exception:
                continue
            continue

        # 3) flashed rectangular pad
        if isinstance(prim, Rectangle) and getattr(prim, "flashed", False):
            try:
                cx, cy = prim.position
                w = prim.width
                h = prim.height
                rect_poly = _rect_to_polygon_mm(cx, cy, w, h)
                polys.append(rect_poly)
            except Exception:
                continue
            continue

        # 4) line segment (trace)
        if isinstance(prim, Line):
            try:
                line_poly = _line_to_polygon_mm(prim)
                if line_poly is not None:
                    polys.append(line_poly)
            except Exception:
                continue
            continue

        # Other primitive types (arcs, text, etc) can be skipped for now.

    return polys


def _inch_to_mm(v: float) -> float:
    return v * 25.4


def _circle_to_polygon_mm(cx_in: float, cy_in: float, r_in: float, segments: int = 16) -> Polygon:
    cx_mm = _inch_to_mm(cx_in)
    cy_mm = _inch_to_mm(cy_in)
    r_mm = _inch_to_mm(r_in)

    verts: List[Point2D] = []
    for i in range(segments):
        angle = 2.0 * math.pi * (i / segments)
        x = cx_mm + r_mm * math.cos(angle)
        y = cy_mm + r_mm * math.sin(angle)
        verts.append(Point2D(x=x, y=y))
    verts.append(verts[0])
    return Polygon(vertices=verts)


def _rect_to_polygon_mm(cx_in: float, cy_in: float, w_in: float, h_in: float) -> Polygon:
    cx_mm = _inch_to_mm(cx_in)
    cy_mm = _inch_to_mm(cy_in)
    w_mm = _inch_to_mm(w_in)
    h_mm = _inch_to_mm(h_in)

    half_w = w_mm / 2.0
    half_h = h_mm / 2.0

    verts = [
        Point2D(cx_mm - half_w, cy_mm - half_h),
        Point2D(cx_mm + half_w, cy_mm - half_h),
        Point2D(cx_mm + half_w, cy_mm + half_h),
        Point2D(cx_mm - half_w, cy_mm + half_h),
        Point2D(cx_mm - half_w, cy_mm - half_h),
    ]
    return Polygon(vertices=verts)


def _line_to_polygon_mm(prim: object) -> Optional[Polygon]:
    """
    Approximate a Gerber Line primitive as a rectangle around the segment.

    We:
    - get start/end points (in inches)
    - determine line width from prim.width or prim.aperture.*
    - build a 4 point polygon that encloses the line as a stadium-like strip
      (but without rounding at the ends).
    """
    if not hasattr(prim, "start") or not hasattr(prim, "end"):
        return None

    x1_in, y1_in = prim.start  # type: ignore[assignment]
    x2_in, y2_in = prim.end    # type: ignore[assignment]

    width_in = getattr(prim, "width", None)
    if width_in is None:
        ap = getattr(prim, "aperture", None)
        if ap is not None:
            width_in = getattr(ap, "width", None)
            if width_in is None:
                width_in = getattr(ap, "diameter", None)
    if width_in is None:
        width_in = 0.005  # ~5 mil fallback

    x1_mm = _inch_to_mm(x1_in)
    y1_mm = _inch_to_mm(y1_in)
    x2_mm = _inch_to_mm(x2_in)
    y2_mm = _inch_to_mm(y2_in)
    width_mm = _inch_to_mm(width_in)

    dx = x2_mm - x1_mm
    dy = y2_mm - y1_mm
    length = math.hypot(dx, dy)
    if length == 0:
        return _circle_to_polygon_mm(x1_in, y1_in, width_in / 2.0)

    ux = -dy / length
    uy = dx / length
    half_w = width_mm / 2.0

    p1 = Point2D(x=x1_mm + ux * half_w, y=y1_mm + uy * half_w)
    p2 = Point2D(x=x1_mm - ux * half_w, y=y1_mm - uy * half_w)
    p3 = Point2D(x=x2_mm - ux * half_w, y=y2_mm - uy * half_w)
    p4 = Point2D(x=x2_mm + ux * half_w, y=y2_mm + uy * half_w)

    verts = [p1, p2, p3, p4, p1]
    return Polygon(vertices=verts)


# ------------------------------
# Naive fallback for outline (no/failed gerber lib)
# ------------------------------


def _populate_outline_polygons_fallback(layer: BoardLayer) -> None:
    """
    Fallback outline polygon extraction using naive X/Y coordinate parsing.
    """
    for f in layer.files:
        polys = _extract_outline_polygons_from_file_fallback(f.path)
        layer.polygons.extend(polys)


def _extract_outline_polygons_from_file_fallback(path: Path) -> List[Polygon]:
    """
    Very naive outline polygon extractor:

    - Looks for lines with X...Y... coordinates
    - Uses header to guess units and format
    - Converts integers with implicit decimals -> mm
    """
    text = path.read_text(errors="ignore")

    fmt = _detect_gerber_format(text)
    scale = _format_to_scale(fmt)

    points: List[Point2D] = []

    coord_re = re.compile(r".*X(-?\d+)Y(-?\d+).*", re.IGNORECASE)

    for line in text.splitlines():
        line = line.strip()
        if "X" not in line or "Y" not in line:
            continue

        m = coord_re.match(line)
        if not m:
            continue

        raw_x = int(m.group(1))
        raw_y = int(m.group(2))

        x_mm = raw_x * scale
        y_mm = raw_y * scale
        points.append(Point2D(x=x_mm, y=y_mm))

    if len(points) < 3:
        return []

    first = points[0]
    last = points[-1]
    if first.x != last.x or first.y != last.y:
        points.append(Point2D(x=first.x, y=first.y))

    return [Polygon(vertices=points)]


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
