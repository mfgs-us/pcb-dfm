"""
Gerber/Excellon parse backend (gerbonara).

The internal parse seam for #3: convert a Gerber layer into our ``Polygon``
model in millimetres, backed by gerbonara (maintained, pure-Python, proper
RS-274X incl. arcs/regions/apertures). Every graphic object is reduced to its
filled outline the same way — ``to_primitives('mm')`` → ``to_arc_poly()`` →
tessellate the segments — so lines get proper (round) end caps, flashes get
their true aperture shape, and *arcs are exact* rather than chord-approximated
as the old pcb-tools path required.

Coordinates come out in mm (gerbonara does the inch↔mm conversion), so callers
must NOT convert again.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .primitives import Point2D, Polygon

try:
    from gerbonara import ExcellonFile, GerberFile
    from gerbonara.utils import MM
    GERBONARA_AVAILABLE = True
except Exception:  # pragma: no cover - defensive
    GerberFile = None  # type: ignore
    ExcellonFile = None  # type: ignore
    MM = None  # type: ignore
    GERBONARA_AVAILABLE = False


def _tool_diameter_mm(tool) -> float:
    """Tool diameter in mm.

    NOTE the unit trap: ``obj.converted('mm')`` converts an object's
    *coordinates* but NOT its shared aperture/tool, whose ``.diameter`` stays in
    the file's native unit. On an inch-native drill file that silently yields
    values 25.4x too small, so convert the tool explicitly.
    """
    if tool is None:
        return 0.0
    try:
        return float(MM(tool.diameter, tool.unit))
    except Exception:
        return 0.0

_ARC_SEGMENTS = 24  # tessellation steps for a full circle


def _tessellate_arc(p1: Tuple[float, float], p2: Tuple[float, float],
                    center: Tuple[float, float], clockwise: bool,
                    full_circle_steps: int = _ARC_SEGMENTS) -> List[Tuple[float, float]]:
    """Points along the arc from p1 to p2 about ``center`` (excluding p1)."""
    cx, cy = center
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    a2 = math.atan2(p2[1] - cy, p2[0] - cx)
    r = math.hypot(p1[0] - cx, p1[1] - cy)
    # Sweep in the correct direction.
    sweep = a2 - a1
    if clockwise:
        while sweep > 0:
            sweep -= 2 * math.pi
        if sweep == 0:
            sweep = -2 * math.pi
    else:
        while sweep < 0:
            sweep += 2 * math.pi
        if sweep == 0:
            sweep = 2 * math.pi
    steps = max(2, int(round(full_circle_steps * abs(sweep) / (2 * math.pi))))
    pts: List[Tuple[float, float]] = []
    for i in range(1, steps + 1):
        a = a1 + sweep * (i / steps)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _arcpoly_points(arc_poly) -> List[Tuple[float, float]]:
    """Flatten a gerbonara ArcPoly outline into (x, y) points, tessellating arcs.

    gerbonara 1.5's ``ArcPoly.approximate_arcs()`` is broken (calls a generator
    property as a method), so we walk ``.segments`` ourselves. Each segment is
    ``(p1, p2, (clockwise, center))``; a straight edge has ``clockwise is None``
    (and ``center == (None, None)``).
    """
    pts: List[Tuple[float, float]] = []
    for seg in arc_poly.segments:
        p1, p2, (clockwise, center) = seg[0], seg[1], seg[2]
        if not pts:
            pts.append((float(p1[0]), float(p1[1])))
        if clockwise is None:
            pts.append((float(p2[0]), float(p2[1])))
        else:
            pts.extend(_tessellate_arc((float(p1[0]), float(p1[1])),
                                       (float(p2[0]), float(p2[1])),
                                       (float(center[0]), float(center[1])),
                                       bool(clockwise)))
    return pts


def _object_polygons_mm(obj) -> List[Polygon]:
    polys: List[Polygon] = []
    try:
        prims = obj.to_primitives("mm")
    except Exception:
        return polys
    for prim in prims:
        try:
            arc_poly = prim.to_arc_poly()
        except Exception:
            continue
        pts = _arcpoly_points(arc_poly)
        if len(pts) >= 3:
            polys.append(Polygon(vertices=[Point2D(x=x, y=y) for x, y in pts]))
    return polys


def gerber_polygons_mm(path: Path) -> List[Polygon]:
    """Parse a Gerber file and return filled outline polygons in mm."""
    if not GERBONARA_AVAILABLE:
        return []
    # Real-world artwork commonly draws pours with zero-size apertures, and
    # gerbonara warns once per occurrence (hundreds per board). The resulting
    # degenerate polygons are filtered downstream, so silence the noise rather
    # than emit thousands of warnings per run.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            gf = GerberFile.open(str(path))
        except Exception:
            return []

        polys: List[Polygon] = []
        for obj in gf.objects:
            polys.extend(_object_polygons_mm(obj))
    return polys


@dataclass
class Trace:
    """A drawn conductor segment in mm (``width_mm`` is the aperture width)."""
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    width_mm: float


def _aperture_width_mm(aperture) -> float:
    """Aperture width in mm.

    Same unit trap as drill tools: an aperture keeps the file's native unit even
    when the owning object is converted, so convert it explicitly.
    """
    if aperture is None:
        return 0.0
    try:
        return float(aperture.equivalent_width(MM))
    except Exception:
        return 0.0


def gerber_traces_mm(path: Path) -> List[Trace]:
    """Drawn segments (traces) with their aperture width, in mm.

    Covers gerbonara ``Line`` *and* ``Arc`` objects — pcb-tools only exposed
    lines, so arc-routed traces were previously invisible to width/spacing
    checks. An arc contributes its endpoints (chord) for position; its width is
    exact.
    """
    if not GERBONARA_AVAILABLE:
        return []
    out: List[Trace] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            gf = GerberFile.open(str(path))
        except Exception:
            return []
        for obj in gf.objects:
            if not (hasattr(obj, "x1") and hasattr(obj, "y2")):
                continue  # Flash / Region: not a drawn segment
            try:
                m = obj.converted("mm")
                width = _aperture_width_mm(getattr(obj, "aperture", None))
                out.append(Trace(
                    x1_mm=float(m.x1), y1_mm=float(m.y1),
                    x2_mm=float(m.x2), y2_mm=float(m.y2), width_mm=width,
                ))
            except Exception:
                continue
    return out


@dataclass
class ApertureInfo:
    """One aperture definition, normalized to mm."""
    code: str                      # D-code, e.g. "D10"
    shape: str                     # circle|rectangle|obround|polygon|macro|unknown
    min_dim_mm: Optional[float]    # smallest positive dimension, None if none
    max_dim_mm: Optional[float]    # largest positive dimension, None if none
    detail: str


def _aperture_dims_mm(ap) -> Tuple[str, List[float], List[str]]:
    """(shape, positive dimensions in mm, notes) for a gerbonara aperture."""
    name = type(ap).__name__
    unit = getattr(ap, "unit", None)
    notes: List[str] = []

    def conv(v, label):
        if v is None:
            return None
        try:
            mm = float(MM(v, unit))
        except Exception:
            return None
        if mm <= 0.0:
            notes.append(f"{label}<=0")
            return None
        return mm

    if "Circle" in name:
        shape, raw = "circle", [("diameter", getattr(ap, "diameter", None))]
    elif "Rectangle" in name:
        shape, raw = "rectangle", [("w", getattr(ap, "w", None)), ("h", getattr(ap, "h", None))]
    elif "Obround" in name:
        shape, raw = "obround", [("w", getattr(ap, "w", None)), ("h", getattr(ap, "h", None))]
    elif "Polygon" in name:
        shape, raw = "polygon", [("diameter", getattr(ap, "diameter", None))]
    elif "Macro" in name:
        # A macro has no scalar size; fall back to its rendered bounding box.
        shape, raw = "macro", []
        try:
            (x0, y0), (x1, y1) = ap.bounding_box(unit)
            raw = [("bbox_w", abs(x1 - x0)), ("bbox_h", abs(y1 - y0))]
        except Exception:
            notes.append("macro bounding box unavailable")
    else:
        shape, raw = "unknown", []
        notes.append(f"unhandled aperture type {name}")

    dims = [d for d in (conv(v, k) for k, v in raw) if d is not None]
    return shape, dims, notes


def gerber_apertures_mm(path: Path) -> Optional[List[ApertureInfo]]:
    """Aperture definitions normalized to mm, or None if the file won't parse.

    gerbonara exposes a *typed* aperture model, so shapes and dimensions are
    read directly instead of sniffing numeric attributes off an untyped object.
    Dimensions are converted explicitly: an aperture keeps the file's native
    unit, so an inch-native file would otherwise report mm values 25.4x small.
    """
    if not GERBONARA_AVAILABLE:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            gf = GerberFile.open(str(path))
            aps = list(gf.apertures())
        except Exception:
            return None

    out: List[ApertureInfo] = []
    for ap in aps:
        shape, dims, notes = _aperture_dims_mm(ap)
        num = getattr(ap, "original_number", None)
        code = f"D{num}" if num is not None else "(unnumbered)"
        if dims:
            detail = f"extracted {len(dims)} dim(s), min={min(dims):.4f}mm, max={max(dims):.4f}mm"
        else:
            detail = "no positive dimension found"
        if notes:
            detail += f" ({', '.join(notes)})"
        out.append(ApertureInfo(
            code=code, shape=shape,
            min_dim_mm=min(dims) if dims else None,
            max_dim_mm=max(dims) if dims else None,
            detail=detail,
        ))
    return out


def gerber_aperture_use_bbox_mm(path: Path, code: str):
    """Bounding box (min_x, min_y, max_x, max_y) in mm of the first object drawn
    with aperture ``code`` (e.g. ``"D10"``), or None.

    Lets a finding about an aperture *definition* be pinned to somewhere that
    aperture is actually used, so violation markers still land on the board.
    """
    if not GERBONARA_AVAILABLE:
        return None
    try:
        num = int(str(code).lstrip("Dd"))
    except (TypeError, ValueError):
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            gf = GerberFile.open(str(path))
        except Exception:
            return None
        for obj in gf.objects:
            ap = getattr(obj, "aperture", None)
            if ap is None or getattr(ap, "original_number", None) != num:
                continue
            try:
                (x0, y0), (x1, y1) = obj.bounding_box(MM)
                return (float(x0), float(y0), float(x1), float(y1))
            except Exception:
                return None
    return None


def gerber_flash_polygons_mm(path: Path) -> List[Polygon]:
    """Filled outlines of *flashed* features only (pads), in mm.

    Flashes are aperture placements — pads — as opposed to drawn Line/Arc
    conductors. Keeping them separate matters on a copper layer, where counting
    traces as pads would skew pad-matching checks.
    """
    if not GERBONARA_AVAILABLE:
        return []
    polys: List[Polygon] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            gf = GerberFile.open(str(path))
        except Exception:
            return []
        for obj in gf.objects:
            # A Flash has a position but no second endpoint.
            if hasattr(obj, "x1") or not hasattr(obj, "x"):
                continue
            polys.extend(_object_polygons_mm(obj))
    return polys


def gerber_edges_mm(path: Path):
    """Outline edges in mm as ``(p_start, p_end, kind, radius, direction)``.

    ``kind`` is ``"line"`` or ``"arc"``; arcs carry their true radius and
    ``"clockwise"``/``"counterclockwise"`` direction. This is the shape the
    outline/contour checks consume, and it is where gerbonara pays off most:
    the pcb-tools path chord-approximated arcs, so a curved board edge or a
    filleted corner lost its radius entirely.
    """
    if not GERBONARA_AVAILABLE:
        return []
    # (start, end, kind, radius, direction) -- radius/direction are None for lines
    edges: List[
        Tuple[Tuple[float, float], Tuple[float, float], str, Optional[float], Optional[str]]
    ] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            gf = GerberFile.open(str(path))
        except Exception:
            return []
        for obj in gf.objects:
            try:
                m = obj.converted("mm")
            except Exception:
                continue
            if not (hasattr(m, "x1") and hasattr(m, "y2")):
                continue  # Flash / Region: not an edge
            start, end = (float(m.x1), float(m.y1)), (float(m.x2), float(m.y2))
            if hasattr(m, "clockwise") and hasattr(m, "cx"):
                center = getattr(m, "center", None)
                if isinstance(center, (tuple, list)) and len(center) == 2:
                    cx, cy = float(center[0]), float(center[1])
                else:  # fall back to the absolute centre fields
                    cx, cy = float(m.cx), float(m.cy)
                radius = math.hypot(start[0] - cx, start[1] - cy)
                direction = "clockwise" if bool(m.clockwise) else "counterclockwise"
                edges.append((start, end, "arc", radius, direction))
            else:
                edges.append((start, end, "line", None, None))
    return edges


# --------------------------------------------------------------------------- #
# Excellon (drills / slots)
# --------------------------------------------------------------------------- #

@dataclass
class DrillHit:
    """A drilled hole in mm."""
    x_mm: float
    y_mm: float
    diameter_mm: float
    plated: Optional[bool] = None


@dataclass
class DrillSlot:
    """A routed slot in mm (``width_mm`` is the routing tool diameter)."""
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    width_mm: float
    plated: Optional[bool] = None


def _open_excellon(path: Path):
    if not GERBONARA_AVAILABLE:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ExcellonFile.open(str(path))
    except Exception:
        return None


def _pcbtools_excellon(path: Path):
    """Compatibility fallback for Excellon gerbonara 1.5 cannot parse.

    gerbonara 1.5 rejects some real-world constructs outright -- notably ``G85``
    routed slots ("Unknown excellon statement"), which fails the *whole file* and
    would otherwise lose its drills too. pcb-tools still reads those, so it is
    retained purely as a fallback so the migration loses no capability. Tracked
    on #3: drop it once gerbonara handles G85 (or we move to 1.6 / Python 3.12).

    Values are normalized to mm via ``to_metric()`` -- deliberately NOT the old
    to_inch()+25.4 path, which double-converted mm-native files.
    """
    try:
        from gerber import excellon  # type: ignore
    except Exception:
        return None
    try:
        ex = excellon.read(str(path))
    except Exception:
        return None
    try:
        ex.to_metric()  # everything in mm from here
    except Exception:
        return None
    return ex


def _pcbtools_hits_mm(path: Path) -> List[DrillHit]:
    ex = _pcbtools_excellon(path)
    if ex is None:
        return []
    out: List[DrillHit] = []
    for hit in getattr(ex, "hits", []) or []:
        try:
            from gerber.excellon import DrillSlot as _PTSlot  # type: ignore
        except Exception:
            _PTSlot = None  # type: ignore
        if _PTSlot is not None and isinstance(hit, _PTSlot):
            continue  # slots are reported by _pcbtools_slots_mm
        try:
            tool = getattr(hit, "tool", None)
            dia = float(getattr(tool, "diameter", 0.0) or 0.0)
            if dia <= 0.0:
                continue
            if hasattr(hit, "position"):
                x, y = hit.position
            else:
                x, y = hit.x, hit.y
            out.append(DrillHit(x_mm=float(x), y_mm=float(y), diameter_mm=dia))
        except Exception:
            continue
    return out


def _pcbtools_slots_mm(path: Path) -> List[DrillSlot]:
    ex = _pcbtools_excellon(path)
    if ex is None:
        return []
    try:
        from gerber.excellon import DrillSlot as _PTSlot  # type: ignore
    except Exception:
        return []
    out: List[DrillSlot] = []
    for hit in getattr(ex, "hits", []) or []:
        if not isinstance(hit, _PTSlot):
            continue
        try:
            tool = getattr(hit, "tool", None)
            width = float(getattr(tool, "diameter", 0.0) or 0.0)
            (x1, y1) = hit.start
            (x2, y2) = hit.end
            out.append(DrillSlot(
                x1_mm=float(x1), y1_mm=float(y1),
                x2_mm=float(x2), y2_mm=float(y2), width_mm=width,
            ))
        except Exception:
            continue
    return out


def excellon_hits_mm(path: Path) -> List[DrillHit]:
    """Drilled holes in mm. Slots are reported by :func:`excellon_slots_mm`."""
    ex = _open_excellon(path)
    if ex is None:
        # gerbonara could not parse this file (e.g. G85 slots) -- fall back so we
        # do not lose its drills entirely.
        return _pcbtools_hits_mm(path)
    hits: List[DrillHit] = []
    try:
        objs = list(ex.drills())
    except Exception:
        try:
            objs = list(ex.objects)
        except Exception:
            return []
    for obj in objs:
        try:
            m = obj.converted("mm")
            dia = _tool_diameter_mm(getattr(obj, "tool", None))
            if dia <= 0.0:
                continue
            hits.append(DrillHit(
                x_mm=float(m.x), y_mm=float(m.y), diameter_mm=dia,
                plated=getattr(m, "plated", None),
            ))
        except Exception:
            continue
    return hits


def excellon_slots_mm(path: Path) -> List[DrillSlot]:
    """Routed slots in mm."""
    ex = _open_excellon(path)
    if ex is None:
        return _pcbtools_slots_mm(path)  # e.g. G85 slots gerbonara rejects
    out: List[DrillSlot] = []
    try:
        slots = list(ex.slots())
    except Exception:
        # gerbonara parsed the file but exposes no slot iterator here; a
        # G85-bearing file would have failed open() and taken the path above.
        return _pcbtools_slots_mm(path)
    for s in slots:
        try:
            m = s.converted("mm")
            width = _tool_diameter_mm(getattr(s, "tool", None))
            out.append(DrillSlot(
                x1_mm=float(m.x1), y1_mm=float(m.y1),
                x2_mm=float(m.x2), y2_mm=float(m.y2),
                width_mm=width, plated=getattr(m, "plated", None),
            ))
        except Exception:
            continue
    return out


def excellon_tool_diameters_mm(path: Path) -> List[float]:
    """Distinct drill tool diameters in mm (hole sizes actually used)."""
    return sorted({h.diameter_mm for h in excellon_hits_mm(path)})
