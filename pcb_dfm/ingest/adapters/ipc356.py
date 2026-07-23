"""IPC-D-356 / IPC-356A netlist adapter.

A netlist is the missing input for a whole class of DFM checks. Without it the
engine cannot tell copper a via *connects to* from a foreign net it must clear,
so checks like ``via_to_copper_clearance`` can only ever be advisory. IPC-D-356
is the common interchange format for exactly this: every CAD tool exports it,
and fabs already consume it for electrical test.

The format is fixed-column, but real exports vary in their padding, so this
parser reads the fields positionally where the standard is reliable (record
type, net name) and by pattern where it is not (coordinates).

Records handled:
  ``317``  through-hole access point (a via or a THT pin)
  ``327``  surface-mount access point (an SMD pad)
Everything else -- comments (``C``), parameters (``P``), adjacency (``037``),
and the ``999`` end marker -- is skipped.

Coordinates are stated relative to whatever origin the CAD tool used, which is
frequently NOT the Gerber origin. See :func:`register_to_board`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..design_model import DesignData, Net, NetPoint

# Location is the FIRST X/Y pair after the access flags; a second pair (and an
# optional R rotation) describes the pad size, which we do not need.
_XY_RE = re.compile(r"X\s*(-?\d+)\s*Y\s*(-?\d+)")
_UNITS_RE = re.compile(r"^P\s+UNITS\s+(\S+)(?:\s+(\d+))?", re.IGNORECASE)

# IPC-D-356 unit codes -> millimetres per integer count.
#   CUST 0 : 0.0001 inch      CUST 1 : 0.00001 inch
#   CUST 2 : 0.001 mm         METRIC : 0.001 mm
_INCH = 25.4
_UNIT_SCALE_MM = {
    ("CUST", "0"): 0.0001 * _INCH,
    ("CUST", "1"): 0.00001 * _INCH,
    ("CUST", "2"): 0.001,
    ("METRIC", None): 0.001,
    ("METRIC", "0"): 0.001,
}
_DEFAULT_SCALE_MM = 0.0001 * _INCH  # the overwhelmingly common CUST 0


def looks_like_ipc356(source: Union[str, Path]) -> bool:
    """True when the file looks like an IPC-D-356 netlist.

    Deliberately content-based: the ``.ipc`` extension is also used by
    IPC-2581, so extension alone would misroute one into the other.
    """
    path = Path(source)
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except OSError:
        return False
    if "IPC-2581" in head:
        return False
    if "IPC-D-356" in head or "IPC-356" in head:
        return True
    # Fall back to structure: netlist records are the bulk of a real file.
    return sum(1 for ln in head.splitlines() if ln[:3] in ("317", "327")) >= 3


def _unit_scale_mm(text: str) -> float:
    for line in text.splitlines():
        m = _UNITS_RE.match(line)
        if not m:
            continue
        system = m.group(1).upper()
        code = m.group(2)
        if (system, code) in _UNIT_SCALE_MM:
            return _UNIT_SCALE_MM[(system, code)]
        if system.startswith("METRIC"):
            return 0.001
        if system.startswith("CUST"):
            return _DEFAULT_SCALE_MM
    return _DEFAULT_SCALE_MM


def from_ipc356(source: Union[str, Path]) -> DesignData:
    """Parse an IPC-D-356 netlist into ``DesignData`` with per-net access points.

    Coordinates are in the netlist's own frame; call :func:`register_to_board`
    to align them with the Gerber geometry.
    """
    path = Path(source)
    text = path.read_text(encoding="utf-8", errors="ignore")
    scale = _unit_scale_mm(text)

    nets: Dict[str, Net] = {}
    for line in text.splitlines():
        rec = line[:3]
        if rec not in ("317", "327"):
            continue
        net_name = line[3:17].strip()
        if not net_name:
            continue

        # Skip past the record's fixed head so the pad-size X/Y cannot be
        # mistaken for the location when a net name is unusually short.
        m = _XY_RE.search(line, 26)
        if m is None:
            m = _XY_RE.search(line)
        if m is None:
            continue

        try:
            x_mm = int(m.group(1)) * scale
            y_mm = int(m.group(2)) * scale
        except ValueError:
            continue

        ref = line[20:26].strip() or None
        pin = line[27:31].strip().lstrip("-") or None

        net = nets.setdefault(net_name, Net(name=net_name))
        net.points.append(NetPoint(
            x_mm=x_mm, y_mm=y_mm,
            kind="through" if rec == "317" else "smd",
            ref=ref, pin=pin,
        ))

    return DesignData(nets=nets)


def register_to_board(
    design: DesignData,
    board_points: List[Tuple[float, float]],
    *,
    tolerance_mm: float = 0.06,
) -> Optional[Tuple[float, float]]:
    """Align netlist coordinates to the board's own frame, in place.

    A netlist's origin is frequently not the Gerber origin -- CAD tools commonly
    emit netlist coordinates relative to the board's lower-left corner. Applying
    the wrong origin silently mislabels every net, which is worse than having no
    netlist at all, so the offset is *derived and verified* rather than assumed:
    every netlist through-hole point is paired against every real drill hit, the
    resulting candidate offsets are voted on, and the winner is applied only if
    it registers a decisive share of the points.

    ``board_points`` are the board's drill hits in mm. Returns the applied
    ``(dx, dy)``, or None when no offset registers enough points (in which case
    the design data is left untouched and callers should treat the netlist as
    unusable for this board).
    """
    through = [
        p for net in design.nets.values() for p in net.points
        if p.kind == "through"
    ]
    if not through or not board_points:
        return None

    # Vote on candidate offsets, quantised so near-identical pairings agree.
    q = max(0.01, tolerance_mm / 2.0)
    votes: Dict[Tuple[float, float], int] = {}
    for p in through:
        for (bx, by) in board_points:
            key = (round((bx - p.x_mm) / q) * q, round((by - p.y_mm) / q) * q)
            votes[key] = votes.get(key, 0) + 1
    if not votes:
        return None

    (dx, dy), _best = max(votes.items(), key=lambda kv: (kv[1], -abs(kv[0][0]) - abs(kv[0][1])))

    # The vote is quantised, so refine it to the mean residual of the pairs it
    # actually registers -- an exact offset rather than a bucket centre.
    residuals: List[Tuple[float, float]] = []
    for p in through:
        for (bx, by) in board_points:
            if abs(p.x_mm + dx - bx) <= tolerance_mm and abs(p.y_mm + dy - by) <= tolerance_mm:
                residuals.append((bx - p.x_mm, by - p.y_mm))
                break
    if residuals:
        dx = sum(r[0] for r in residuals) / len(residuals)
        dy = sum(r[1] for r in residuals) / len(residuals)

    matched = sum(
        1 for p in through
        if any(abs(p.x_mm + dx - bx) <= tolerance_mm and abs(p.y_mm + dy - by) <= tolerance_mm
               for (bx, by) in board_points)
    )
    # Require a decisive majority: a netlist for a *different* board can still
    # produce a few coincidental pairings, and a partly-wrong origin is worse
    # than none.
    if matched < max(3, int(0.6 * len(through))):
        return None

    if dx or dy:
        for net in design.nets.values():
            for p in net.points:
                p.x_mm += dx
                p.y_mm += dy
    return (dx, dy)
