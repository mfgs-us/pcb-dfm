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
  * a path to an ODB++ job         -> adapters.odbpp
  * a path to an IPC-D-356 netlist -> adapters.ipc356

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


# Extensions worth sniffing when a design-data file is BUNDLED inside a Gerber
# package. Detection is content-based (the extensions overlap between formats),
# so this only narrows the set of files to open.
_DESIGN_DATA_EXTS = {".ipc", ".ipc356", ".ipcd356", ".xml", ".cvg", ".ipc2581"}


def discover_design_data(root_dir: Union[str, Path]) -> Optional[Path]:
    """Find a design-data file bundled alongside Gerbers in a package.

    Fabs routinely ship the netlist (usually IPC-D-356) inside the same archive
    as the artwork, since they consume it for electrical test. When the caller
    gave no explicit ``--design-data`` we look for one here, so the net-aware and
    footprint-aware checks light up for the most common real-world input rather
    than only when a flag is passed.

    Preference order is by richness: IPC-2581 (stackup + nets + routed geometry)
    over an IPC-D-356 netlist (access points only). Detection is by CONTENT, so
    a misnamed or unrelated ``.xml`` is not mistaken for design data. Returns the
    path, or None when nothing usable is bundled.

    Auto-adoption is safe because the downstream registration fails closed: an
    IPC-D-356 netlist that does not register onto the board's own drill hits is
    refused rather than applied, so a stray netlist for a different board cannot
    silently mislabel this one.
    """
    root = Path(root_dir)
    if not root.is_dir():
        return None

    candidates = [
        p for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix.lower() in _DESIGN_DATA_EXTS
    ]

    ipc2581: Optional[Path] = None
    ipc356: Optional[Path] = None
    for p in candidates:
        try:
            if ipc2581 is None and _looks_like_ipc2581_content(p):
                ipc2581 = p
            elif ipc356 is None and looks_like_ipc356(p):
                ipc356 = p
        except OSError:
            continue

    return ipc2581 or ipc356


def _looks_like_ipc2581_content(path: Path) -> bool:
    """Content-based IPC-2581 detection, for discovery only.

    The shared ``looks_like_ipc2581`` accepts any ``.xml`` by extension, which is
    right when a user names a file explicitly but wrong for auto-discovery -- an
    unrelated ``notes.xml`` sitting in a package must not be adopted as design
    data. So here we require the format marker to actually be in the file.
    """
    head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    return "IPC-2581" in head


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
