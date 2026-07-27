"""Outer-layer copper balance (warp / plating symmetry).

Distinct from ``copper_density_balance`` (which measures *local* density deltas
across a single layer): this compares the *whole-layer* copper coverage of the
two outer layers. Grossly asymmetric outer copper (e.g. a bare top over a solid
bottom plane) warps on reflow and plates unevenly, because it is the outer
layers that get electroplated.

Deliberately conservative and advisory:
  * only the TOP vs BOTTOM outer layers are compared -- inner planes legitimately
    dwarf signal layers, so an all-layer spread would false-positive on every
    board with a ground plane.
  * coverage is an exact union rasterisation (no double-counting of overlapping
    pours/traces/pads), intersected with the board outline.
  * it never hard-fails; a very lopsided board (default > 65 pp difference) is a
    warning worth a designer's glance, not a fab reject.

Not applicable without both a top and a bottom copper layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Set, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import outline_contours_mm
from ..geometry.net_map import _rasterize_fill
from ..results import CheckResult, MetricResult, Violation

_CELL_MM = 0.5


def _param(ctx: CheckContext) -> float:
    p = (ctx.check_def.raw or {}).get("params", {}) or {}
    return float(p.get("outer_balance_pp", 65.0))


def _na(ctx: CheckContext, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.ratio_percent(None, target_pct=None, limit_high_pct=None),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


def _poly_cells(poly, cell: float) -> Set[Tuple[int, int]]:
    """Cells covered by one copper polygon: exterior fill minus its own holes."""
    ext = [(v.x, v.y) for v in poly.vertices]
    cells = _rasterize_fill(ext, cell)
    for hole in getattr(poly, "holes", []) or []:
        if len(hole) >= 3:
            cells -= _rasterize_fill([(p.x, p.y) for p in hole], cell)
    return cells


def _side_cells(layers, cell: float) -> Set[Tuple[int, int]]:
    out: Set[Tuple[int, int]] = set()
    for ly in layers:
        for poly in getattr(ly, "polygons", []):
            if len(poly.vertices) >= 3:
                out |= _poly_cells(poly, cell)
    return out


def _board_cells(ctx: CheckContext, cell: float) -> Set[Tuple[int, int]]:
    """Cells inside the board: largest outline contour minus internal cutouts."""
    outline_file = next(
        (f for f in ctx.ingest.files if f.layer_type == "outline"), None)
    if outline_file is not None:
        contours = outline_contours_mm(Path(outline_file.path))
        if contours:
            cells = _rasterize_fill(contours[0], cell)
            for cut in contours[1:]:
                cells -= _rasterize_fill(cut, cell)
            return cells
    # Fallback: the geometry bounding box.
    bb = ctx.geometry.board_bounds()
    if bb is None:
        return set()
    from math import floor
    cells = set()
    for ix in range(floor(bb.min_x / cell), floor(bb.max_x / cell) + 1):
        for iy in range(floor(bb.min_y / cell), floor(bb.max_y / cell) + 1):
            cells.add((ix, iy))
    return cells


@register_check("copper_balance_plating")
def run_copper_balance_plating(ctx: CheckContext) -> CheckResult:
    threshold = _param(ctx)

    copper = ctx.geometry.get_layers_by_type("copper")

    def _side(ly) -> str:
        return str(getattr(ly, "side", "") or "").lower()

    top = [ly for ly in copper if _side(ly) == "top"]
    bottom = [ly for ly in copper if _side(ly) == "bottom"]
    if not top or not bottom:
        return _na(ctx, "Need both a top and a bottom copper layer; outer-copper balance not applicable.")

    board = _board_cells(ctx, _CELL_MM)
    if len(board) < 16:  # too small/absent to estimate coverage meaningfully
        return _na(ctx, "Board area could not be estimated; outer-copper balance not applicable.")
    nboard = float(len(board))

    top_cov = 100.0 * len(_side_cells(top, _CELL_MM) & board) / nboard
    bot_cov = 100.0 * len(_side_cells(bottom, _CELL_MM) & board) / nboard
    balance = abs(top_cov - bot_cov)

    status = "warning" if balance > threshold else "pass"
    heavier = "top" if top_cov > bot_cov else "bottom"

    violations: List[Violation] = []
    if status == "warning":
        violations.append(Violation(
            severity="warning",
            message=(
                f"Outer-copper coverage is lopsided: top {top_cov:.0f}% vs "
                f"bottom {bot_cov:.0f}% ({balance:.0f} pp, {heavier} heavier, "
                f"> {threshold:.0f} pp) -> warp / uneven outer-layer plating risk. "
                f"Add copper thieving/​hatch to the lighter side to balance."
            ),
            location=None,
        ))
    else:
        violations.append(Violation(
            severity="info",
            message=f"Outer copper balanced: top {top_cov:.0f}% vs bottom {bot_cov:.0f}% ({balance:.0f} pp).",
            location=None,
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=100.0 if status == "pass" else 60.0,
        metric=MetricResult.ratio_percent(
            float(balance), target_pct=threshold, limit_high_pct=threshold),
        violations=violations,
    ).finalize()
