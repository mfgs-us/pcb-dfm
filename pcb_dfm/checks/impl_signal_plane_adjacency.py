"""Signal layer reference-plane adjacency (stackup structure).

On a 4+ layer board every signal layer should sit next to a solid reference
plane, or its return current has no clean path. Planes are identified by copper
coverage (a plane fills most of the board; a signal layer does not), so this
works from the copper geometry alone -- no stackup file needed. On <4 layers
adjacency is trivial, so the check is not_applicable.
"""

from __future__ import annotations

import re
from math import floor
from typing import List, Set, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.net_map import _rasterize_fill
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na

_CELL_MM = 0.5


def _layer_order_key(layer) -> Tuple[int, int]:
    side = str(getattr(layer, "side", "") or "").lower()
    if side == "top":
        return (0, 0)
    if side == "bottom":
        return (2, 0)
    m = re.search(r"(\d+)", getattr(layer, "logical_layer", "") or "")
    return (1, int(m.group(1)) if m else 0)


def _coverage(layer, board_cells: Set[Tuple[int, int]]) -> float:
    if not board_cells:
        return 0.0
    cells: Set[Tuple[int, int]] = set()
    for poly in getattr(layer, "polygons", []):
        if len(poly.vertices) >= 3:
            cells |= _rasterize_fill([(v.x, v.y) for v in poly.vertices], _CELL_MM)
            for hole in getattr(poly, "holes", []) or []:
                if len(hole) >= 3:
                    cells -= _rasterize_fill([(p.x, p.y) for p in hole], _CELL_MM)
    return len(cells & board_cells) / len(board_cells)


@register_check("signal_plane_adjacency")
def run_signal_plane_adjacency(ctx: CheckContext) -> CheckResult:
    geom = ctx.geometry
    copper = geom.get_layers_by_type("copper") if geom is not None else []
    if len(copper) < 4:
        return na(ctx, "Fewer than 4 copper layers; plane adjacency is trivial / not applicable.")
    plane_cov = float((ctx.check_def.raw.get("params", {}) or {}).get("plane_coverage", 0.65))

    bb = geom.board_bounds()
    if bb is None:
        return na(ctx, "No board extent; not applicable.")
    board = {(ix, iy)
             for ix in range(floor(bb.min_x / _CELL_MM), floor(bb.max_x / _CELL_MM) + 1)
             for iy in range(floor(bb.min_y / _CELL_MM), floor(bb.max_y / _CELL_MM) + 1)}
    if len(board) < 16:
        return na(ctx, "Board too small to estimate coverage; not applicable.")

    ordered = sorted(copper, key=_layer_order_key)
    is_plane = [_coverage(ly, board) >= plane_cov for ly in ordered]

    orphan_signal: List[str] = []
    for i, ly in enumerate(ordered):
        if is_plane[i]:
            continue
        adj_plane = (i > 0 and is_plane[i - 1]) or (i + 1 < len(ordered) and is_plane[i + 1])
        if not adj_plane:
            orphan_signal.append(getattr(ly, "logical_layer", None) or f"layer{i}")
    if not any(is_plane):
        return na(ctx, "No reference plane identified in the stack; cannot assess adjacency.")
    flagged = bool(orphan_signal)
    msg = (f"Signal layer(s) with no adjacent reference plane: {', '.join(orphan_signal)} "
           f"-> no clean return path.") if flagged \
        else "Every signal layer is adjacent to a reference plane."
    return advisory(ctx, flagged, count_metric(len(orphan_signal)), msg)
