"""
Adapter: BOM (CSV) -> DesignData components (identity only).

A BOM comes from the schematic/PLM, not the layout, so it carries *identity*
(part number, class, do-not-populate) keyed by reference designator — the thing
bare placement lacks. This adapter parses a tolerant CSV subset into
placement-less :class:`Component` rows; ``design_data.merge_bom`` then joins them
onto placement by refdes.

Tolerances (BOMs are messy in practice):
  * a preamble before the header row is skipped (header is the first row with a
    recognizable designator column);
  * columns are matched by a normalized alias table (case/spacing-insensitive);
  * one row lists many designators -- ``"R1, R2 R3; R5-R7"`` -- expanded,
    including ``R1-R3`` / ``R1-3`` ranges;
  * do-not-populate is read from a ``DNP`` column or an inverted
    ``Populate``/``Fitted`` column.

Part class and polarity are derived heuristically (designator prefix +
description), so any check consuming them stays ``heuristic``.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Dict, List, Optional, Union

from ..design_model import Component, DesignData


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Normalized-header -> canonical field.
_ALIASES: Dict[str, str] = {}
for _field, _names in {
    "designators": ["designator", "designators", "refdes", "reference", "references",
                    "partreference", "partreferences"],
    "value": ["value", "comment", "val"],
    "part_number": ["mpn", "manufacturerpartnumber", "mfrpart", "mfrpartnumber",
                    "partnumber", "manufacturerpn", "mfrpn"],
    "manufacturer": ["manufacturer", "mfr", "mfg"],
    "quantity": ["qty", "quantity"],
    "dnp": ["dnp", "donotpopulate", "nostuff", "nopop"],
    "populate": ["populate", "fitted", "fit", "install"],
    "footprint": ["footprint", "package", "pattern"],
    "description": ["description", "desc"],
    "height": ["height", "heightmm"],
}.items():
    for _n in _names:
        _ALIASES[_norm(_n)] = _field


_PREFIX_CLASS = {
    "LED": "led", "FB": "ferrite", "CN": "connector", "TP": "testpoint",
    "SW": "switch", "R": "resistor", "C": "capacitor", "L": "inductor",
    "D": "diode", "U": "ic", "Q": "transistor", "J": "connector", "P": "connector",
    "Y": "crystal", "X": "crystal", "K": "relay", "F": "fuse",
}
_POLAR_CLASSES = {"led", "diode", "ic", "connector", "transistor", "relay"}
_TRUE = {"y", "yes", "true", "1", "x", "dnp", "np"}
_FALSE = {"n", "no", "false", "0", ""}


def _prefix(ref: str) -> str:
    m = re.match(r"^([A-Za-z]+)", ref.strip())
    return m.group(1).upper() if m else ""


def _part_class(ref: str, description: Optional[str]) -> Optional[str]:
    pre = _prefix(ref)
    # Longest known prefix first (LED before L, CN before C).
    for key in sorted(_PREFIX_CLASS, key=len, reverse=True):
        if pre == key:
            cls = _PREFIX_CLASS[key]
            break
    else:
        cls = None
    d = (description or "").lower()
    if cls == "capacitor" and re.search(r"tantalum|electrolyt|polar", d):
        return "electrolytic"
    return cls


def _polarized(part_class: Optional[str], description: Optional[str]) -> Optional[bool]:
    d = (description or "").lower()
    if re.search(r"tantalum|electrolyt|polar", d):
        return True
    if part_class in _POLAR_CLASSES or part_class == "electrolytic":
        return True
    if part_class in ("resistor", "inductor", "ferrite", "crystal", "capacitor",
                      "fuse", "testpoint"):
        return False
    return None


def _truthy(v: Optional[str]) -> bool:
    return _norm(v or "") in {_norm(t) for t in _TRUE}


def expand_designators(cell: str) -> List[str]:
    """Expand a designator cell into individual refs (ranges + separators)."""
    out: List[str] = []
    seen = set()
    for tok in re.split(r"[,\s;]+", (cell or "").strip()):
        if not tok:
            continue
        m = re.match(r"^([A-Za-z]+)(\d+)-([A-Za-z]*)(\d+)$", tok)
        expanded = [tok]
        if m:
            pre, a, pre2, b = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
            if (not pre2 or pre2.upper() == pre.upper()) and a <= b:
                expanded = [f"{pre}{i}" for i in range(a, b + 1)]
        for ref in expanded:
            key = ref.upper()
            if key not in seen:
                seen.add(key)
                out.append(ref)
    return out


def _find_header(rows: List[List[str]]) -> Optional[int]:
    """Index of the first row that carries a designator column."""
    for i, row in enumerate(rows):
        if any(_ALIASES.get(_norm(c)) == "designators" for c in row):
            return i
    return None


def bom_components(text: str) -> List[Component]:
    """Parse BOM CSV text into placement-less, identity-carrying components."""
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any((c or "").strip() for c in r)]  # drop blank lines
    hi = _find_header(rows)
    if hi is None:
        return []

    header = rows[hi]
    col: Dict[str, int] = {}
    for idx, head_cell in enumerate(header):
        field = _ALIASES.get(_norm(head_cell))
        if field and field not in col:
            col[field] = idx

    def _get(row: List[str], field: str) -> Optional[str]:
        i = col.get(field)
        if i is None or i >= len(row):
            return None
        v = (row[i] or "").strip()
        return v or None

    out: List[Component] = []
    for row in rows[hi + 1:]:
        desig = _get(row, "designators")
        if not desig:
            continue
        description = _get(row, "description")
        # DNP: explicit DNP column wins; else an inverted Populate/Fitted column.
        dnp = False
        if "dnp" in col:
            dnp = _truthy(_get(row, "dnp"))
        elif "populate" in col:
            pop = _get(row, "populate")
            dnp = pop is not None and not _truthy(pop)

        height = None
        h = _get(row, "height")
        if h is not None:
            try:
                height = float(re.sub(r"[^0-9.]+", "", h) or "nan")
                if height != height:  # NaN
                    height = None
            except ValueError:
                height = None

        for ref in expand_designators(desig):
            cls = _part_class(ref, description)
            out.append(Component(
                ref=ref,
                value=_get(row, "value"),
                footprint=_get(row, "footprint"),
                part_number=_get(row, "part_number"),
                manufacturer=_get(row, "manufacturer"),
                description=description,
                part_class=cls,
                polarized=_polarized(cls, description),
                dnp=dnp,
                height_mm=height,
                placed=False,
            ))
    return out


def from_bom(source: Union[str, Path]) -> DesignData:
    """Load a BOM CSV file into a DesignData carrying identity-only components."""
    text = Path(source).read_text(encoding="utf-8-sig")
    return DesignData(components=bom_components(text), source="bom")
