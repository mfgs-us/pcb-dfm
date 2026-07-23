"""A small, self-contained Excellon reader for files gerbonara cannot parse.

gerbonara 1.5 rejects ``G85`` routed slots outright ("Unknown excellon
statement"), and because that aborts the *whole file* the drills go with it.
This module is the fallback for exactly that case (#17), replacing the
pcb-tools dependency the migration in #3 otherwise had to keep alive.

Scope is deliberately narrow: enough Excellon to recover drill hits and slots
from a real fabrication drill file. It is *not* a general Excellon
implementation and is never used when gerbonara parses the file successfully.

What it handles:
  - ``M48`` header terminated by ``%`` / ``M95``
  - units from ``METRIC`` / ``INCH`` (with ``,LZ`` / ``,TZ`` and an optional
    explicit ``000.000`` format) and from ``M71`` / ``M72`` mid-body
  - implied-decimal coordinates under either zero-suppression convention, as
    well as ordinary explicit-decimal coordinates
  - tool definitions (``T01C0.500``) and tool selection (``T01``)
  - modal coordinates (an omitted X or Y keeps its previous value)
  - ``G85`` slots, both inline (``X1Y1G85X2Y2``) and split across lines
  - route-mode slots (``M15`` tool-down ... ``G01`` moves ... ``M16`` tool-up)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_INCH_TO_MM = 25.4

# Default implied-decimal formats when the header does not state one.
# Excellon convention: metric 3.3, imperial 2.4.
_DEFAULT_FORMAT = {"mm": (3, 3), "inch": (2, 4)}

_COORD_RE = re.compile(r"([XY])([-+]?[0-9]*\.?[0-9]+)")
_TOOL_DEF_RE = re.compile(r"^T(\d+).*?C([0-9]*\.?[0-9]+)", re.IGNORECASE)
_TOOL_SEL_RE = re.compile(r"^T(\d+)\s*$", re.IGNORECASE)
_EXPLICIT_FORMAT_RE = re.compile(r"(\d+)\.(\d+)")


@dataclass
class RawHit:
    x_mm: float
    y_mm: float
    diameter_mm: float


@dataclass
class RawSlot:
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    width_mm: float


@dataclass
class ParsedExcellon:
    hits: List[RawHit] = field(default_factory=list)
    slots: List[RawSlot] = field(default_factory=list)


class _State:
    """Mutable parse state: units, coordinate format, tools, position."""

    def __init__(self) -> None:
        self.units = "inch"  # Excellon's default when the header is silent
        self.zero_suppression: Optional[str] = None  # "LZ" | "TZ"
        self.fmt: Optional[Tuple[int, int]] = None  # (int_digits, dec_digits)
        # Tool diameters are stored in mm at definition time, since the header
        # units are what they were declared in.
        self.tools: Dict[str, float] = {}
        self.current_tool: Optional[str] = None
        self.x: float = 0.0
        self.y: float = 0.0
        self.have_pos = False
        self.route_down = False

    @property
    def tool_dia_mm(self) -> float:
        if self.current_tool is None:
            return 0.0
        return self.tools.get(self.current_tool, 0.0)

    def to_mm(self, value: float) -> float:
        return value * _INCH_TO_MM if self.units == "inch" else value

    def parse_coord(self, token: str) -> float:
        """Convert one coordinate token to mm, honouring implied decimals."""
        if "." in token:
            return self.to_mm(float(token))

        sign = 1.0
        if token and token[0] in "+-":
            sign = -1.0 if token[0] == "-" else 1.0
            token = token[1:]
        if not token:
            return 0.0

        int_digits, dec_digits = self.fmt or _DEFAULT_FORMAT[self.units]
        width = int_digits + dec_digits

        # LZ = leading zeros present, so it is the *trailing* zeros that were
        # dropped -> pad on the right. TZ is the mirror image. With neither
        # stated, assume the (far more common) leading-zero-suppressed form.
        if self.zero_suppression == "LZ":
            token = token.ljust(width, "0")
        else:
            token = token.rjust(width, "0")

        value = int(token) / (10.0 ** dec_digits)
        return self.to_mm(sign * value)


def _strip_comment(line: str) -> str:
    i = line.find(";")
    return line if i == -1 else line[:i]


def _apply_units_directive(st: _State, body: str) -> None:
    """Handle a ``METRIC``/``INCH`` header line and its trailing options."""
    upper = body.upper()
    st.units = "mm" if "METRIC" in upper else "inch"
    for part in upper.split(",")[1:]:
        part = part.strip()
        if part in ("LZ", "TZ"):
            st.zero_suppression = part
        else:
            m = _EXPLICIT_FORMAT_RE.search(part)
            if m:
                st.fmt = (len(m.group(1)), len(m.group(2)))


def _coords(st: _State, chunk: str) -> Optional[Tuple[float, float]]:
    """Read a modal X/Y pair from ``chunk``; None if it carries no coordinate."""
    found = _COORD_RE.findall(chunk)
    if not found:
        return None
    x, y = st.x, st.y
    for axis, token in found:
        if axis == "X":
            x = st.parse_coord(token)
        else:
            y = st.parse_coord(token)
    return x, y


def parse_excellon_mm(path: Path) -> Optional[ParsedExcellon]:
    """Parse an Excellon file into mm-space hits and slots.

    Returns ``None`` if the file cannot be read or contains no usable tool
    definitions, so callers can distinguish "not parseable" from "genuinely
    empty" and avoid silently reporting a board as having no drills.
    """
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return None

    st = _State()
    out = ParsedExcellon()
    in_header = False
    saw_tool_def = False

    for raw_line in text.splitlines():
        line = _strip_comment(raw_line).strip()
        if not line:
            continue

        upper = line.upper()

        if upper.startswith("M48"):
            in_header = True
            continue
        if upper in ("%", "M95"):
            in_header = False
            continue
        if upper.startswith("M30") or upper.startswith("M00"):
            break

        if upper.startswith("METRIC") or upper.startswith("INCH"):
            _apply_units_directive(st, line)
            continue
        if upper.startswith("M71"):
            st.units = "mm"
            continue
        if upper.startswith("M72"):
            st.units = "inch"
            continue

        # Tool definition (header) vs tool selection (body). A definition is
        # recognised by its C<diameter> parameter, wherever it appears.
        m = _TOOL_DEF_RE.match(line)
        if m:
            code = str(int(m.group(1)))
            try:
                dia = float(m.group(2))
            except ValueError:
                continue
            st.tools[code] = st.to_mm(dia)
            saw_tool_def = True
            if in_header:
                continue
            # Some files define and select in one statement outside the header.
            st.current_tool = code
            continue

        m = _TOOL_SEL_RE.match(line)
        if m:
            code = str(int(m.group(1)))
            # T0 is "no tool" / end of drilling.
            st.current_tool = None if code == "0" else code
            continue

        if in_header:
            continue  # remaining header directives are not needed here

        # --- body ---
        if upper.startswith("M15"):
            st.route_down = True
            continue
        if upper.startswith("M16") or upper.startswith("M17"):
            st.route_down = False
            continue

        dia = st.tool_dia_mm

        if "G85" in upper:
            head, _, tail = upper.partition("G85")
            start = _coords(st, head)
            if start is not None:
                st.x, st.y = start
                st.have_pos = True
            end = _coords(st, tail)
            if end is not None and st.have_pos and dia > 0.0:
                out.slots.append(RawSlot(
                    x1_mm=st.x, y1_mm=st.y,
                    x2_mm=end[0], y2_mm=end[1],
                    width_mm=dia,
                ))
                st.x, st.y = end
            continue

        pos = _coords(st, upper)
        if pos is None:
            continue

        is_rapid = upper.startswith("G00")
        prev = (st.x, st.y)
        had_pos = st.have_pos
        st.x, st.y = pos
        st.have_pos = True

        if st.route_down:
            # Tool is down: this move cuts a slot of the tool's width.
            if not is_rapid and had_pos and dia > 0.0 and prev != pos:
                out.slots.append(RawSlot(
                    x1_mm=prev[0], y1_mm=prev[1],
                    x2_mm=pos[0], y2_mm=pos[1],
                    width_mm=dia,
                ))
            continue

        if is_rapid:
            continue  # positioning move, not a hole
        if dia > 0.0:
            out.hits.append(RawHit(x_mm=st.x, y_mm=st.y, diameter_mm=dia))

    if not saw_tool_def:
        return None
    return out
