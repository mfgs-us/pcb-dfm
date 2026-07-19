"""
Adapter: IPC-2581 (B) XML -> DesignData.

IPC-2581 is a single-file, open-standard ECAD/MCAD exchange XML. Its full schema
is large; this adapter targets a documented, pragmatic SUBSET sufficient for the
stackup- and net-aware DFM checks:

  * Stackup   -- <StackupLayer> entries (thickness in mm, plus a dielectric
                 constant and a copper/dielectric classification, resolved from
                 the layer's own attributes or a referenced <Layer> definition).
  * Nets      -- <LogicalNet> names, optional controlled-impedance hints on the
                 net, and routed length summed from <Set net="..."> geometry
                 (<Line>/<Arc>) under each <LayerFeature>.
  * Diff pairs -- explicit <DiffPair> elements, else inferred from +/- / _P/_N
                 net-name pairing.

Parsing is namespace-agnostic (matches on local tag names), so both namespaced
and bare fixtures work. Real vendor exports carry this data in richer
constructs (Spec references, net classes); extend the resolvers below as needed.
Lengths/thicknesses are interpreted as millimetres.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Union

from ..design_model import (
    ControlledImpedanceSpec,
    DesignData,
    DiffPair,
    Net,
    NetFeature,
    Stackup,
    StackupLayer,
)

_COPPER_FUNCS = {"CONDUCTOR", "SIGNAL", "PLANE", "MIXED", "COPPER", "POWER_GROUND"}
_DIELECTRIC_FUNCS = {"DIELECTRIC", "PREPREG", "CORE", "SUBSTRATE", "DIELPREPREG"}


def _ln(el: ET.Element) -> str:
    """Local tag name, ignoring XML namespace."""
    return el.tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> List[ET.Element]:
    return [e for e in root.iter() if _ln(e) == name]


def _fattr(el: ET.Element, *names: str) -> Optional[float]:
    for n in names:
        v = el.attrib.get(n)
        if v is not None:
            try:
                return float(v)
            except ValueError:
                continue
    return None


def looks_like_ipc2581(source: Union[str, Path]) -> bool:
    path = Path(source)
    if path.suffix.lower() in {".xml", ".cvg", ".ipc", ".ipc2581"}:
        return True
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:2048]
    except OSError:
        return False
    return "IPC-2581" in head


def _parse_stackup(root: ET.Element) -> Optional[Stackup]:
    stackup_els = _find_all(root, "Stackup")
    if not stackup_els:
        return None

    # Layer-function map from <Layer name=".." layerFunction=".."> definitions.
    func_map: Dict[str, str] = {}
    for lyr in _find_all(root, "Layer"):
        name = lyr.attrib.get("name") or lyr.attrib.get("layerOrGroupRef")
        func = lyr.attrib.get("layerFunction") or lyr.attrib.get("type")
        if name and func:
            func_map[name] = func.upper()

    layers: List[StackupLayer] = []
    for sl in _find_all(stackup_els[0], "StackupLayer"):
        ref = sl.attrib.get("layerOrGroupRef") or sl.attrib.get("name") or f"layer_{len(layers)}"
        thickness = _fattr(sl, "thickness", "thicknessMm", "thickness_mm")
        er = _fattr(sl, "dielectricConstant", "er", "epsilonR")
        func = (sl.attrib.get("type") or sl.attrib.get("layerFunction")
                or func_map.get(ref, "")).upper()

        if func in _COPPER_FUNCS:
            kind = "copper"
        elif func in _DIELECTRIC_FUNCS:
            kind = "dielectric"
        else:
            # No explicit function: an Er implies dielectric, otherwise copper.
            kind = "dielectric" if er is not None else "copper"

        layers.append(StackupLayer(name=ref, kind=kind, thickness_mm=thickness, er=er))

    return Stackup(layers=layers) if layers else None


def _line_segment(el: ET.Element):
    """Return ((sx, sy), (ex, ey)) for a Line/Arc, or None. Arcs are taken as
    their chord (endpoints) -- a documented approximation."""
    sx = _fattr(el, "startX")
    sy = _fattr(el, "startY")
    ex = _fattr(el, "endX")
    ey = _fattr(el, "endY")
    if None in (sx, sy, ex, ey):
        return None
    return ((sx, sy), (ex, ey))


def _seg_len(seg) -> float:
    (sx, sy), (ex, ey) = seg
    return math.hypot(ex - sx, ey - sy)


def _set_width(st: ET.Element) -> Optional[float]:
    """Trace width from a lineWidth/width attribute on any descendant (e.g. a
    <LineDesc lineWidth="...">), if present."""
    for e in st.iter():
        w = _fattr(e, "lineWidth", "lineWidthMm", "width")
        if w is not None:
            return w
    return None


def _parse_nets(root: ET.Element) -> (
    "tuple[Dict[str, Net], List[ControlledImpedanceSpec]]"
):
    nets: Dict[str, Net] = {}
    ci: List[ControlledImpedanceSpec] = []

    def _ensure(name: str) -> Net:
        if name not in nets:
            nets[name] = Net(name=name)
        return nets[name]

    # Logical nets: names, class, and optional impedance hints.
    for ln_el in _find_all(root, "LogicalNet"):
        name = ln_el.attrib.get("name")
        if not name:
            continue
        net = _ensure(name)
        net.net_class = ln_el.attrib.get("netClass") or net.net_class
        target = _fattr(ln_el, "targetImpedanceOhm", "targetOhm", "impedanceOhm")
        if target is not None:
            ci.append(ControlledImpedanceSpec(
                name=name,
                target_ohm=target,
                width_mm=_fattr(ln_el, "traceWidthMm", "widthMm", "width"),
                tolerance_pct=_fattr(ln_el, "tolerancePct", "toleranceP") or 10.0,
            ))

    # Routed geometry: capture <Line>/<Arc> segments per <Set net="..."> per
    # layer, keeping both the summed length and the actual segments (for
    # geometry-aware checks like diff-pair spacing).
    for lf in _find_all(root, "LayerFeature"):
        layer = lf.attrib.get("layerRef") or lf.attrib.get("layer")
        for st in _find_all(lf, "Set"):
            net_name = st.attrib.get("net")
            if not net_name:
                continue
            segments = []
            total = 0.0
            for e in st.iter():
                if _ln(e) in ("Line", "Arc"):
                    seg = _line_segment(e)
                    if seg is not None:
                        segments.append(seg)
                        total += _seg_len(seg)
            if total > 0.0:
                _ensure(net_name).features.append(NetFeature(
                    layer=layer, length_mm=total,
                    width_mm=_set_width(st), segments=segments))

    return nets, ci


def _infer_diff_pairs(nets: Dict[str, Net]) -> List[DiffPair]:
    """Pair nets by common +/- / _P/_N / P|N suffix conventions."""
    suffixes = [("+", "-"), ("_P", "_N"), ("_p", "_n"), ("P", "N")]
    pairs: List[DiffPair] = []
    used: set = set()
    for name in sorted(nets):
        if name in used:
            continue
        for pos_suf, neg_suf in suffixes:
            if name.endswith(pos_suf):
                base = name[: -len(pos_suf)]
                partner = base + neg_suf
                if partner in nets and partner not in used:
                    pairs.append(DiffPair(name=base.rstrip("_") or name,
                                          positive=name, negative=partner))
                    used.add(name)
                    used.add(partner)
                    break
    return pairs


def _parse_diff_pairs(root: ET.Element, nets: Dict[str, Net]) -> List[DiffPair]:
    explicit: List[DiffPair] = []
    for dp in _find_all(root, "DiffPair"):
        pos = dp.attrib.get("positive") or dp.attrib.get("plus")
        neg = dp.attrib.get("negative") or dp.attrib.get("minus")
        if pos and neg:
            explicit.append(DiffPair(
                name=dp.attrib.get("name", f"{pos}/{neg}"),
                positive=pos, negative=neg,
                target_ohm=_fattr(dp, "targetOhm", "targetImpedanceOhm"),
            ))
    return explicit if explicit else _infer_diff_pairs(nets)


def from_ipc2581(source: Union[str, Path]) -> DesignData:
    path = Path(source)
    root = ET.fromstring(path.read_text(encoding="utf-8"))

    nets, ci = _parse_nets(root)
    dd = DesignData(
        stackup=_parse_stackup(root),
        nets=nets,
        controlled_impedance=ci,
        diff_pairs=_parse_diff_pairs(root, nets),
        source="ipc2581",
    )
    return dd
