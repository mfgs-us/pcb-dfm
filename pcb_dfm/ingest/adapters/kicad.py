"""
Adapter: KiCad project / board -> DesignData.

A KiCad ``.kicad_pcb`` file carries exactly the design *intent* bare Gerbers
lack — nets, routed geometry, the physical stackup, and component placement — so
it is a natural DesignData source. This adapter keeps the engine's invariant
that **Gerbers remain the geometry-of-record** (the poured copper the fab
receives): it reads the KiCad project only for ``DesignData``, not for the
BoardGeometry the checks measure. A full KiCad-native geometry path (rendering
tracks/pads/zones, with the zone-fill-staleness caveat) is deliberately out of
scope here and tracked as a future issue.

Parsed, pragmatically, without requiring KiCad installed:

  * Stackup   -- ``(setup (stackup (layer "F.Cu" (type "copper") (thickness ..)
                 (epsilon_r ..)) ...))``. Copper vs dielectric by layer type;
                 mask/silk/paste layers are ignored.
  * Nets      -- the ``(net N "name")`` table, with routed length + segments
                 summed from ``(segment ...)`` / ``(arc ...)`` per net (arcs are
                 taken as their chord). Feeds diff-pair spacing/skew today.
  * Net class -- from board ``(net_class ... (add_net "X"))`` blocks (KiCad 6)
                 and/or ``net_settings`` in a sibling ``.kicad_pro`` (KiCad 7+),
                 applied to nets by glob pattern.
  * Diff pairs -- inferred from +/- / _P/_N net-name conventions.
  * Components -- ``(footprint ...)`` placements (refdes, value, x/y/rotation,
                 side) into the provisional ``DesignData.components``.

Coordinates and thicknesses are millimetres (KiCad's native board unit).
"""

from __future__ import annotations

import fnmatch
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..design_model import (
    Component,
    DesignData,
    Net,
    NetFeature,
    Stackup,
    StackupLayer,
)
from .ipc2581 import _infer_diff_pairs

# A parsed S-expression node: a list whose items are strings (atoms / quoted
# strings) or nested nodes.
SNode = List[object]


# --------------------------------------------------------------------------- #
# Minimal S-expression reader (dependency-free)
# --------------------------------------------------------------------------- #

def _parse_sexpr(text: str) -> SNode:
    """Parse a single top-level S-expression into nested lists of str/list."""
    stack: List[SNode] = []
    root: Optional[SNode] = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "(":
            node: SNode = []
            if stack:
                stack[-1].append(node)
            else:
                root = node
            stack.append(node)
            i += 1
        elif c == ")":
            if stack:
                stack.pop()
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                buf.append(text[i])
                i += 1
            i += 1  # closing quote
            if stack:
                stack[-1].append("".join(buf))
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            if stack:
                stack[-1].append(text[i:j])
            i = j
    if root is None:
        raise ValueError("no S-expression found")
    return root


def _tag(node) -> Optional[str]:
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return None


def _tagged(node: SNode, tag: str) -> List[SNode]:
    """Direct child nodes whose head atom == tag."""
    return [c for c in node if isinstance(c, list) and _tag(c) == tag]


def _first(node: SNode, tag: str) -> Optional[SNode]:
    for c in node:
        if isinstance(c, list) and _tag(c) == tag:
            return c
    return None


def _atoms(node: SNode) -> List[str]:
    return [c for c in node[1:] if isinstance(c, str)]


def _fatom(node: Optional[SNode], idx: int = 0) -> Optional[float]:
    if node is None:
        return None
    atoms = _atoms(node)
    if idx >= len(atoms):
        return None
    try:
        return float(atoms[idx])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Detection + path resolution
# --------------------------------------------------------------------------- #

def looks_like_kicad(source: Union[str, Path]) -> bool:
    path = Path(source)
    if path.is_dir():
        return any(path.glob("*.kicad_pcb"))
    return path.suffix.lower() in {".kicad_pcb", ".kicad_pro"}


def _resolve_board(path: Path) -> Tuple[Path, Optional[Path]]:
    """Return (board_pcb_path, project_pro_path_or_None)."""
    if path.is_dir():
        pcbs = sorted(path.glob("*.kicad_pcb"))
        if not pcbs:
            raise ValueError(f"no .kicad_pcb found in project directory: {path}")
        board = pcbs[0]
    elif path.suffix.lower() == ".kicad_pro":
        board = path.with_suffix(".kicad_pcb")
        if not board.exists():
            raise ValueError(f"no board next to project file: {board}")
    else:
        board = path
    pro = board.with_suffix(".kicad_pro")
    return board, (pro if pro.exists() else None)


# --------------------------------------------------------------------------- #
# Section parsers
# --------------------------------------------------------------------------- #

def _parse_stackup(root: SNode) -> Optional[Stackup]:
    setup = _first(root, "setup")
    stackup = _first(setup, "stackup") if setup else None
    if stackup is None:
        return None
    layers: List[StackupLayer] = []
    for ly in _tagged(stackup, "layer"):
        name = ly[1] if len(ly) > 1 and isinstance(ly[1], str) else f"layer_{len(layers)}"
        type_node = _first(ly, "type")
        ltype = (_atoms(type_node)[0].lower() if type_node and _atoms(type_node) else "")
        thickness = _fatom(_first(ly, "thickness"))
        er = _fatom(_first(ly, "epsilon_r"))
        if "copper" in ltype:
            kind = "copper"
        elif any(k in ltype for k in ("core", "prepreg", "dielectric")):
            kind = "dielectric"
        else:
            # solder mask / silkscreen / paste layers are not part of the
            # electrical stack the checks reason about.
            continue
        layers.append(StackupLayer(name=name, kind=kind, thickness_mm=thickness, er=er))
    return Stackup(layers=layers) if layers else None


def _side_of_layer(layer: Optional[str]) -> Optional[str]:
    if not layer:
        return None
    if layer.startswith("F."):
        return "top"
    if layer.startswith("B."):
        return "bottom"
    return None


def _parse_nets_and_routes(root: SNode) -> Dict[str, Net]:
    # Net number -> name.
    num_to_name: Dict[str, str] = {}
    for net_el in _tagged(root, "net"):
        atoms = _atoms(net_el)
        if len(atoms) >= 2:
            num_to_name[atoms[0]] = atoms[1]

    # Every named net in the table exists, even if unrouted -- net-class
    # assignment and net presence must not depend on having copper drawn yet.
    nets: Dict[str, Net] = {
        name: Net(name=name) for name in num_to_name.values() if name
    }

    def _ensure(name: str) -> Net:
        if name not in nets:
            nets[name] = Net(name=name)
        return nets[name]

    def _add_route(el: SNode) -> None:
        net_el = _first(el, "net")
        if net_el is None:
            return
        net_num = (_atoms(net_el)[0] if _atoms(net_el) else None)
        name = num_to_name.get(net_num or "")
        if not name:  # unconnected / net 0
            return
        start, end = _first(el, "start"), _first(el, "end")
        sx, sy = _fatom(start, 0), _fatom(start, 1)
        ex, ey = _fatom(end, 0), _fatom(end, 1)
        if sx is None or sy is None or ex is None or ey is None:
            return
        seg = ((sx, sy), (ex, ey))
        length = math.hypot(ex - sx, ey - sy)
        layer_node = _first(el, "layer")
        layer = _atoms(layer_node)[0] if layer_node and _atoms(layer_node) else None
        _ensure(name).features.append(NetFeature(
            layer=layer,
            length_mm=length,
            width_mm=_fatom(_first(el, "width")),
            segments=[seg],
        ))

    for seg_el in _tagged(root, "segment"):
        _add_route(seg_el)
    for arc_el in _tagged(root, "arc"):
        _add_route(arc_el)  # chord approximation via start/end

    return nets


def _apply_board_netclasses(root: SNode, nets: Dict[str, Net]) -> None:
    """KiCad 6 board files carry (net_class "Name" ... (add_net "X")) blocks."""
    for nc in _tagged(root, "net_class"):
        cls = nc[1] if len(nc) > 1 and isinstance(nc[1], str) else None
        if not cls:
            continue
        for add in _tagged(nc, "add_net"):
            atoms = _atoms(add)
            if atoms and atoms[0] in nets:
                nets[atoms[0]].net_class = cls


def _apply_project_netclasses(pro: Path, nets: Dict[str, Net]) -> None:
    """KiCad 7+ keeps net classes in the .kicad_pro; assign by glob pattern."""
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    net_settings = (data.get("net_settings") or {}) if isinstance(data, dict) else {}
    for pat in net_settings.get("netclass_patterns", []) or []:
        cls = pat.get("netclass")
        glob = pat.get("pattern")
        if not cls or not glob:
            continue
        for name, net in nets.items():
            if net.net_class is None and fnmatch.fnmatch(name, glob):
                net.net_class = cls


def _parse_components(root: SNode) -> List[Component]:
    comps: List[Component] = []
    for fp in _tagged(root, "footprint"):
        footprint = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else None
        at = _first(fp, "at")
        x, y = _fatom(at, 0), _fatom(at, 1)
        rot = _fatom(at, 2) or 0.0
        layer_node = _first(fp, "layer")
        layer = _atoms(layer_node)[0] if layer_node and _atoms(layer_node) else None

        ref: Optional[str] = None
        value: Optional[str] = None
        for prop in _tagged(fp, "property"):
            atoms = _atoms(prop)
            if len(atoms) >= 2:
                if atoms[0] == "Reference":
                    ref = atoms[1]
                elif atoms[0] == "Value":
                    value = atoms[1]
        # KiCad 5 fallback: (fp_text reference R1 ...) / (fp_text value 10k ...)
        if ref is None or value is None:
            for ft in _tagged(fp, "fp_text"):
                atoms = _atoms(ft)
                if len(atoms) >= 2 and atoms[0] == "reference" and ref is None:
                    ref = atoms[1]
                elif len(atoms) >= 2 and atoms[0] == "value" and value is None:
                    value = atoms[1]

        if ref is None:
            continue
        comps.append(Component(
            ref=ref, value=value, footprint=footprint,
            x_mm=x, y_mm=y, rotation_deg=rot, side=_side_of_layer(layer),
        ))
    return comps


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def from_kicad(source: Union[str, Path]) -> DesignData:
    board, pro = _resolve_board(Path(source))
    root = _parse_sexpr(board.read_text(encoding="utf-8"))

    nets = _parse_nets_and_routes(root)
    _apply_board_netclasses(root, nets)
    if pro is not None:
        _apply_project_netclasses(pro, nets)

    return DesignData(
        stackup=_parse_stackup(root),
        nets=nets,
        diff_pairs=_infer_diff_pairs(nets),
        components=_parse_components(root),
        source="kicad",
    )
