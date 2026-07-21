"""
Deterministic synthetic board builders shared by the property-based and golden
regression tests.

Boards are emitted as RS-274X / Excellon text and zipped at runtime (no binary
fixtures committed). A board is described by primitives in mm, and an optional
affine transform (scale / translate / mirror) is applied at emit time so tests
can assert geometric invariants.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Tuple

_MM = 1_000_000  # format 4.6 -> integer token is mm * 1e6

Transform = Callable[[float, float], Tuple[float, float]]


def identity(x: float, y: float) -> Tuple[float, float]:
    return x, y


@dataclass
class Trace:
    x0: float
    y0: float
    x1: float
    y1: float
    width_mm: float


@dataclass
class Pad:
    cx: float
    cy: float
    w: float
    h: float


@dataclass
class Hole:
    x: float
    y: float
    dia_mm: float


@dataclass
class Board:
    outline: List[Tuple[float, float]]      # closed polygon vertices (mm)
    traces: List[Trace] = field(default_factory=list)
    pads: List[Pad] = field(default_factory=list)
    holes: List[Hole] = field(default_factory=list)
    inner_planes: int = 0                    # number of inner copper planes


def _coord(v: float) -> str:
    return str(int(round(v * _MM)))


def _emit_copper(board: Board, tf: Transform, wscale: float) -> str:
    lines = ["%FSLAX46Y46*%", "%MOMM*%"]
    aps: dict = {}
    body: List[str] = []

    def ap_for(kind: str, *dims: float) -> str:
        key = (kind, tuple(round(d, 6) for d in dims))
        if key not in aps:
            code = 10 + len(aps)
            if kind == "C":
                lines.append(f"%ADD{code}C,{dims[0]:.6f}*%")
            else:
                lines.append(f"%ADD{code}R,{dims[0]:.6f}X{dims[1]:.6f}*%")
            aps[key] = f"D{code}*"
        return aps[key]

    for t in board.traces:
        sel = ap_for("C", t.width_mm * wscale)
        (sx, sy), (ex, ey) = tf(t.x0, t.y0), tf(t.x1, t.y1)
        body.append(sel)
        body.append(f"X{_coord(sx)}Y{_coord(sy)}D02*")
        body.append(f"X{_coord(ex)}Y{_coord(ey)}D01*")
    for p in board.pads:
        sel = ap_for("R", p.w * wscale, p.h * wscale)
        cx, cy = tf(p.cx, p.cy)
        body.append(sel)
        body.append(f"X{_coord(cx)}Y{_coord(cy)}D03*")

    lines.extend(body)
    lines.append("M02*")
    return "\n".join(lines) + "\n"


def _emit_outline(board: Board, tf: Transform) -> str:
    lines = ["%FSLAX46Y46*%", "%MOMM*%", "%ADD10C,0.100000*%", "D10*"]
    pts = [tf(x, y) for (x, y) in board.outline]
    x0, y0 = pts[0]
    lines.append(f"X{_coord(x0)}Y{_coord(y0)}D02*")
    for (x, y) in pts[1:]:
        lines.append(f"X{_coord(x)}Y{_coord(y)}D01*")
    lines.append(f"X{_coord(x0)}Y{_coord(y0)}D01*")
    lines.append("M02*")
    return "\n".join(lines) + "\n"


def _emit_drill(board: Board, tf: Transform, wscale: float) -> str:
    lines = ["M48", "METRIC,TZ"]
    tools: dict = {}
    for h in board.holes:
        d = round(h.dia_mm * wscale, 4)
        if d not in tools:
            tools[d] = len(tools) + 1
            lines.append(f"T{tools[d]}C{d:.4f}")
    lines.append("%")
    for tnum in sorted(set(tools.values())):
        lines.append(f"T{tnum}")
        for h in board.holes:
            if tools[round(h.dia_mm * wscale, 4)] == tnum:
                x, y = tf(h.x, h.y)
                lines.append(f"X{x:.4f}Y{y:.4f}")
    lines.append("T0")
    lines.append("M30")
    return "\n".join(lines) + "\n"


def emit_zip(
    board: Board,
    tmp_path: Path,
    *,
    transform: Transform = identity,
    wscale: float = 1.0,
    name: str = "board.zip",
) -> Path:
    copper = _emit_copper(board, transform, wscale)
    files = {
        "board-F_Cu.gbr": copper,
        "board-B_Cu.gbr": copper,
        "board-F_Mask.gbr": copper,
        "board-F_Silkscreen.gbr": copper,
        "board-Edge_Cuts.gbr": _emit_outline(board, transform),
    }
    for i in range(1, board.inner_planes + 1):
        files[f"board-In{i}_Cu.gbr"] = copper
    if board.holes:
        files["board.drl"] = _emit_drill(board, transform, wscale)
    z = tmp_path / name
    with zipfile.ZipFile(z, "w") as zf:
        for fn, content in files.items():
            zf.writestr(fn, content)
    return z


# --------------------------------------------------------------------------
# Named archetypes for the golden regression corpus
# --------------------------------------------------------------------------

def clean_two_layer() -> Board:
    """Comfortable 2-layer board: wide traces, generous pads/holes."""
    return Board(
        outline=[(0, 0), (20, 0), (20, 14), (0, 14)],
        traces=[Trace(2, 3, 16, 3, 0.30), Trace(2, 7, 16, 7, 0.30)],
        pads=[Pad(4, 10, 1.2, 1.2), Pad(14, 10, 1.2, 1.2)],
        holes=[Hole(4, 10, 0.6), Hole(14, 10, 0.6)],
    )


def four_layer_planes() -> Board:
    """4-layer board with GND/PWR inner planes (exercises stackup detection)."""
    b = clean_two_layer()
    b.inner_planes = 2
    return b


def thin_trace_board() -> Board:
    """Known-bad: a 0.05 mm trace violates min trace width."""
    return Board(
        outline=[(0, 0), (18, 0), (18, 10), (0, 10)],
        traces=[Trace(2, 5, 16, 5, 0.05)],
        pads=[Pad(9, 8, 1.0, 1.0)],
        holes=[Hole(9, 8, 0.5)],
    )


ARCHETYPES = {
    "clean_two_layer": clean_two_layer,
    "four_layer_planes": four_layer_planes,
    "thin_trace_board": thin_trace_board,
}


def result_digest(result, ndigits: int = 4) -> dict:
    """A normalized, platform-stable projection of a DfmResult for golden
    comparison: drops timestamps/paths and rounds floats so the same input
    yields the same digest across machines."""
    def rnd(v):
        return round(v, ndigits) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    checks = []
    for cat in result.categories:
        for c in cat.checks:
            m = c.metric
            checks.append({
                "id": c.check_id,
                "status": c.status,
                "severity": c.severity,
                "confidence": c.confidence,
                "measured": rnd(m.measured_value) if m else None,
                "violations": len(c.violations),
            })
    checks.sort(key=lambda d: d["id"])
    return {
        "overall": {"status": result.summary.status, "score": rnd(result.summary.overall_score)},
        "stackup_layers": result.design.stackup_layers,
        "warnings": sorted(result.warnings),
        "checks": checks,
    }
