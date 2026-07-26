"""Design advisory: global fiducial coverage for automated assembly.

Pick-and-place aligns a board from global fiducials -- round copper dots with an
oversized mask opening, no drill, isolated from other copper. An SMT board with
fewer than three of them (non-collinear) can't be optically aligned. Heuristic:
fiducials are inferred from geometry, so this warns, never fails, and stays
quiet on non-SMT (through-hole) boards where fiducials are not needed.
"""

from __future__ import annotations

from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, excellon_hits_mm
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds
from ..ingest.design_intel import classify_component
from ..results import CheckResult, MetricResult, Violation
from ._design_advisory import bbox_center, na

_FID_MIN_DIA_MM = 0.6
_FID_MAX_DIA_MM = 2.5
_FID_MAX_ASPECT = 1.4      # round-ish
_MIN_FIDUCIALS = 3


def _design_fiducials(ctx: CheckContext) -> int:
    """Count fiducials the design data already identifies (refdes/footprint), or
    -1 when there is no placement data to consult. Reliable when present -- the
    artwork heuristic below is only needed for Gerber-only boards."""
    dd = getattr(ctx, "design_data", None)
    comps = getattr(dd, "components", None) if dd is not None else None
    if not comps:
        return -1
    return sum(
        1 for c in comps
        if getattr(c, "placed", True)
        and classify_component(c)[0] == "fiducial"
        and (c.pads or (c.x_mm is not None and c.y_mm is not None))
    )


def _has_smt_assembly(ctx: CheckContext) -> bool:
    """A solder-paste layer means SMT parts -> the board needs fiducials.

    Paste layers classify as `other` in the ingest (not their own type), so we
    detect them by filename ('*paste*', or the .gtp/.gbp extensions) rather than
    by geometry layer type.
    """
    for f in getattr(ctx.ingest, "files", None) or []:
        nm = (getattr(f, "original_name", "") or "").lower()
        if "paste" in nm or nm.endswith((".gtp", ".gbp")):
            return True
    return False


@register_check("fiducial_coverage")
def run_fiducial_coverage(ctx: CheckContext) -> CheckResult:
    geom = ctx.geometry
    copper_layers = queries.get_copper_layers(geom)
    if not copper_layers:
        return na(ctx, "No copper layers to detect fiducials.")

    if not _has_smt_assembly(ctx):
        return na(ctx, "No SMT (solder-paste) assembly detected; global fiducials "
                       "are not required for a through-hole board.")

    # Prefer the design data's own fiducial identity (refdes/footprint) -- the
    # artwork heuristic below misses fiducials that sit inside a ground pour, and
    # a placement file states them exactly. Only when it actually FINDS fiducials,
    # though: a bare netlist (IPC-D-356) omits fiducials entirely (they carry no
    # net), so a count of 0 there must fall through to artwork detection rather
    # than wrongly report "no fiducials".
    design_count = _design_fiducials(ctx)
    if design_count > 0:
        return _result(ctx, design_count)

    holes: List[Bounds] = []
    if GERBONARA_AVAILABLE:
        for f in getattr(ctx.ingest, "files", None) or []:
            if getattr(f, "layer_type", None) == "drill":
                for h in excellon_hits_mm(f.path):
                    r = 0.5 * h.diameter_mm
                    holes.append(Bounds(h.x_mm - r, h.y_mm - r, h.x_mm + r, h.y_mm + r))
    hole_index = PolygonIndex.from_bounds(list(enumerate(holes))) if holes else None

    fiducials: List[Tuple[float, float]] = []
    for layer in copper_layers:
        polys = list(layer.polygons)
        bounds = [p.bounds() for p in polys]
        idx = PolygonIndex.from_bounds(list(enumerate(bounds)))
        for i, poly in enumerate(polys):
            b = bounds[i]
            w = b.max_x - b.min_x
            h = b.max_y - b.min_y
            if w <= 0 or h <= 0:
                continue
            short, long_ = min(w, h), max(w, h)
            if short < _FID_MIN_DIA_MM or long_ > _FID_MAX_DIA_MM:
                continue
            if (long_ / short if short > 0 else 99) > _FID_MAX_ASPECT:
                continue
            # A fiducial carries no drill (that would be a via/pad) and is
            # isolated (its own mask keep-out separates it from other copper).
            if hole_index is not None and hole_index.query_bbox(b):
                continue
            if [j for j in idx.query_bbox(b) if j != i]:
                continue
            cx, cy = bbox_center(b)
            fiducials.append((cx, cy))

    return _result(ctx, len(fiducials))


def _result(ctx: CheckContext, count: int) -> CheckResult:
    ok = count >= _MIN_FIDUCIALS
    status = "pass" if ok else "warning"
    sev = "info" if ok else "warning"
    msg = (f"{count} global fiducial(s) present."
           if ok else
           f"Only {count} global fiducial(s) on an SMT board; automated placement "
           f"wants >= {_MIN_FIDUCIALS} non-collinear fiducials.")
    return CheckResult(
        check_id=ctx.check_def.id, name=ctx.check_def.name,
        category_id=ctx.check_def.category_id, status=status, severity=sev,
        score=100.0 if ok else 60.0,
        metric=MetricResult(kind="count", units="count",
                            measured_value=float(count), target=float(_MIN_FIDUCIALS)),
        violations=[Violation(severity=sev, message=msg, location=None)],
    ).finalize()
