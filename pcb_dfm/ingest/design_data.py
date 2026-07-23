"""
Design-data loading: normalize any supported input into the internal
``DesignData`` model.

Supported sources (see also ``pcb_dfm.ingest.adapters``):
  * ``None``                      -> None
  * a ``DesignData`` instance     -> returned as-is
  * a ``dict``                    -> JSON sidecar shape (adapters.sidecar)
  * a path to ``*.json``          -> parsed then treated as the sidecar shape
  * a path to IPC-2581 XML        -> adapters.ipc2581
  * a KiCad project dir / .kicad_pcb / .kicad_pro -> adapters.kicad
  * a path to ODB++               -> not yet implemented (planned adapter)

Bare Gerbers carry no connectivity or stackup, so this is how the impedance,
dielectric-uniformity, and differential-pair checks obtain the data they need.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .adapters import (
    from_bom,
    from_ipc356,
    from_ipc2581,
    from_kicad,
    from_odbpp,
    from_sidecar,
    looks_like_ipc356,
    looks_like_ipc2581,
    looks_like_kicad,
    looks_like_odbpp,
)
from .design_model import DesignData

DesignDataLike = Union[DesignData, Dict[str, Any], str, Path, None]


def merge_bom(base: DesignData, bom: DesignData) -> DesignData:
    """Layer BOM identity onto placement, joining by reference designator.

    Placement is authoritative for geometry; the BOM is authoritative for
    identity (part number, class, DNP, ...). Designators present only in the BOM
    are added as un-placed components; designators only in placement are kept
    as-is. Mismatches are recorded in ``base.warnings``.
    """
    by_ref = {c.ref.upper(): c for c in base.components}
    bom_refs = set()
    for b in bom.components:
        bom_refs.add(b.ref.upper())
        existing = by_ref.get(b.ref.upper())
        if existing is None:
            base.components.append(b)  # in BOM, not laid out
            continue
        # Enrich identity in place; keep placement geometry.
        existing.part_number = b.part_number or existing.part_number
        existing.manufacturer = b.manufacturer or existing.manufacturer
        existing.description = b.description or existing.description
        existing.part_class = b.part_class or existing.part_class
        if b.polarized is not None:
            existing.polarized = b.polarized
        existing.dnp = b.dnp or existing.dnp
        existing.height_mm = b.height_mm if b.height_mm is not None else existing.height_mm
        if not existing.value:
            existing.value = b.value

    placed_only = [c.ref for c in base.components if c.placed and c.ref.upper() not in bom_refs]
    bom_only = [b.ref for b in bom.components if b.ref.upper() not in by_ref]
    if placed_only:
        base.warnings.append(
            f"{len(placed_only)} placed component(s) not found in BOM: "
            f"{', '.join(sorted(placed_only)[:10])}"
            + (" …" if len(placed_only) > 10 else ""))
    if bom_only:
        base.warnings.append(
            f"{len(bom_only)} BOM line(s) not placed on the board: "
            f"{', '.join(sorted(bom_only)[:10])}"
            + (" …" if len(bom_only) > 10 else ""))
    return base


def load_design_data(source: DesignDataLike, *, bom: DesignDataLike = None) -> Optional[DesignData]:
    dd = _load_one(source)
    if bom is not None:
        bom_dd = bom if isinstance(bom, DesignData) else from_bom(str(bom))
        dd = merge_bom(dd or DesignData(), bom_dd)
    return dd


def _load_one(source: DesignDataLike) -> Optional[DesignData]:
    if source is None:
        return None
    if isinstance(source, DesignData):
        return source
    if isinstance(source, dict):
        return from_sidecar(source)

    path = Path(source)
    if not path.exists():
        raise ValueError(f"design-data file not found: {path}")

    # ODB++ before KiCad: a job is a directory, and so is a KiCad project, so
    # the structural check has to come first.
    if looks_like_odbpp(path):
        return from_odbpp(path)

    if looks_like_kicad(path):
        return from_kicad(path)

    # Before IPC-2581: both formats use the .ipc extension, so order matters
    # and both detectors are content-based.
    if looks_like_ipc356(path):
        return from_ipc356(path)

    if looks_like_ipc2581(path):
        return from_ipc2581(path)

    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("design-data JSON must be an object at the top level")
        return from_sidecar(data)

    raise ValueError(
        f"unrecognized design-data source: {path} "
        f"(expected a KiCad project/.kicad_pcb, a .json sidecar, an IPC-2581 .xml, "
        f"an IPC-D-356 netlist, or an ODB++ job)"
    )
