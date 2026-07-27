"""Castellated / edge-plated hole geometry.

A castellation is a plated hole the board outline routes *through*, leaving a
plated half-barrel on the board edge (edge-connector fingers, solder-on modules).
The fab concerns are geometric and unambiguous once a hole actually crosses the
outline:

  * **Sliver / breakout** -- if the routed edge leaves less than half the barrel
    in copper (the hole centre sits outside the board material), the remaining
    plated wall is a thin sliver that lifts or breaks out. A well-formed
    castellation is bisected at or inside its centre.
  * **Pitch** -- castellations packed tighter than the plating/routing process
    allows bridge together.

Only plated holes that genuinely cross an outline contour are considered, so an
ordinary internal hole near (but not crossing) the edge never trips this -- that
clearance is ``copper_to_edge_distance``'s job. A board with no edge-crossing
plated hole is ``not_applicable`` (the common case).
"""

from __future__ import annotations

from math import hypot
from pathlib import Path
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import (
    GERBONARA_AVAILABLE,
    excellon_hits_mm,
    outline_contours_mm,
)
from ..geometry.primitives import Point2D
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_min_annular_ring import _min_distance_to_polygon_edges, _point_in_polygon


def _params(ctx: CheckContext) -> Tuple[float, float]:
    p = (ctx.check_def.raw or {}).get("params", {}) or {}
    return float(p.get("sliver_tolerance_mm", 0.1)), float(p.get("min_pitch_mm", 1.0))


def _na(ctx: CheckContext, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult(kind="count", units="count", measured_value=None,
                            target=0.0, limit_high=0.0),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


@register_check("castellated_edge_plating")
def run_castellated_edge_plating(ctx: CheckContext) -> CheckResult:
    sliver_tol, min_pitch = _params(ctx)

    if not GERBONARA_AVAILABLE:
        return _na(ctx, "Gerber/Excellon parser unavailable; castellation geometry not checked.")

    # Board outline contours. Largest = boundary; the rest are internal cutouts.
    outline_file = next(
        (f for f in ctx.ingest.files if f.layer_type == "outline"), None)
    if outline_file is None:
        return _na(ctx, "No board outline layer; castellation geometry not applicable.")
    contours = outline_contours_mm(Path(outline_file.path))
    if not contours:
        return _na(ctx, "Board outline present but no closed contour; not applicable.")
    boundary = [Point2D(x, y) for (x, y) in contours[0]]
    cutouts = [[Point2D(x, y) for (x, y) in c] for c in contours[1:]]
    contour_pts = [boundary] + cutouts

    # Plated drills only. plated=None (a bare PTH file) is treated as plated.
    drills: List[Tuple[float, float, float]] = []  # (x, y, diameter)
    for f in ctx.ingest.files:
        if f.layer_type != "drill":
            continue
        for h in excellon_hits_mm(Path(f.path)):
            if h.plated is False:
                continue
            if h.diameter_mm <= 0.0:
                continue
            drills.append((h.x_mm, h.y_mm, h.diameter_mm))
    if not drills:
        return _na(ctx, "No plated drills; castellation geometry not applicable.")

    # Castellations = plated holes an outline contour passes through (edge within
    # the hole radius of the centre).
    castellations: List[Tuple[float, float, float]] = []  # (x, y, diameter)
    for (x, y, dia) in drills:
        r = 0.5 * dia
        d_edge = min(_min_distance_to_polygon_edges(x, y, pts) for pts in contour_pts)
        if d_edge < r:
            castellations.append((x, y, dia))
    if not castellations:
        return _na(ctx, "No plated hole crosses the board outline; castellation geometry not applicable.")

    slivers: List[Tuple[float, float, str]] = []   # (x, y, note)
    for (x, y, dia) in castellations:
        # Copper material = inside the boundary AND outside every cutout.
        in_material = _point_in_polygon(x, y, boundary) and not any(
            _point_in_polygon(x, y, c) for c in cutouts)
        if in_material:
            continue  # centre in copper -> at least half the barrel remains
        # Centre is outside the copper: how far past the edge? Beyond the
        # tolerance the remaining barrel is a thin, breaking-out sliver.
        d_edge = min(_min_distance_to_polygon_edges(x, y, pts) for pts in contour_pts)
        if d_edge > sliver_tol:
            slivers.append((
                x, y,
                f"barrel {d_edge:.2f} mm past the edge (< half remains)"))

    # Pitch: adjacent castellation centres closer than the floor bridge.
    tight_pairs: List[Tuple[float, float, float]] = []  # (x, y, pitch)
    for i in range(len(castellations)):
        xi, yi, _di = castellations[i]
        for j in range(i + 1, len(castellations)):
            xj, yj, _dj = castellations[j]
            d = hypot(xi - xj, yi - yj)
            if d < min_pitch:
                tight_pairs.append((0.5 * (xi + xj), 0.5 * (yi + yj), d))

    violations: List[Violation] = []
    worst: Optional[Tuple[float, float]] = None
    for (x, y, note) in slivers:
        violations.append(Violation(
            severity=ctx.check_def.raw.get("severity_default", "warning"),
            message=f"Edge-plated hole at ({x:.2f}, {y:.2f}) is not cleanly bisected: {note} -> plating breakout/​sliver.",
            location=ViolationLocation(layer="Outline", x_mm=x, y_mm=y,
                                       notes="Malformed castellation."),
        ))
        if worst is None:
            worst = (x, y)
    for (x, y, pitch) in tight_pairs:
        violations.append(Violation(
            severity=ctx.check_def.raw.get("severity_default", "warning"),
            message=f"Castellations near ({x:.2f}, {y:.2f}) are {pitch:.2f} mm centre-to-centre (< {min_pitch:.2f} mm) -> plating-bridge risk.",
            location=ViolationLocation(layer="Outline", x_mm=x, y_mm=y,
                                       notes="Castellation pitch too tight."),
        ))
        if worst is None:
            worst = (x, y)

    bad = len(slivers) + len(tight_pairs)
    status = "warning" if bad else "pass"
    if status == "pass":
        violations = [Violation(
            severity="info",
            message=f"{len(castellations)} castellation(s) found; all cleanly bisected and adequately spaced.",
            location=None,
        )]
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=100.0 if status == "pass" else 60.0,
        metric=MetricResult(kind="count", units="count",
                            measured_value=float(bad), target=0.0, limit_high=0.0),
        violations=violations,
    ).finalize()
