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
    NetPoint,
    Pad,
    Stackup,
    StackupLayer,
    Via,
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


def _net_number_table(root: SNode) -> Dict[str, str]:
    """The ``(net N "name")`` number->name table.

    Present in KiCad 5-8 board files; **dropped in the 20260623 (KiCad 10.99)
    format**, where net references on pads/segments carry the name directly. An
    empty table therefore signals the newer name-only convention.
    """
    num_to_name: Dict[str, str] = {}
    for net_el in _tagged(root, "net"):
        atoms = _atoms(net_el)
        if len(atoms) >= 2:
            num_to_name[atoms[0]] = atoms[1]
    return num_to_name


def _resolve_net_name(net_el: Optional[SNode], num_to_name: Dict[str, str]) -> Optional[str]:
    """The net name from a ``(net ...)`` reference, across format versions.

    Handles ``(net N "name")`` (inline name), ``(net N)`` (number -> table
    lookup, old segments), and ``(net "name")`` (name-only, KiCad 10.99). Net 0 /
    the empty name resolves to None (unconnected).
    """
    if net_el is None:
        return None
    atoms = _atoms(net_el)
    if not atoms:
        return None
    if len(atoms) >= 2:
        return atoms[1] or None  # (net N "name")
    tok = atoms[0]
    if num_to_name:
        return num_to_name.get(tok) or None  # old numbered ref -> table
    return tok or None  # new name-only ref


def _parse_nets_and_routes(root: SNode, num_to_name: Dict[str, str]) -> Dict[str, Net]:
    # Every named net in the table exists, even if unrouted -- net-class
    # assignment and net presence must not depend on having copper drawn yet.
    # (In the newer name-only format the table is empty and nets are discovered
    # from the references on segments / vias / pads instead.)
    nets: Dict[str, Net] = {
        name: Net(name=name) for name in num_to_name.values() if name
    }

    def _ensure(name: str) -> Net:
        if name not in nets:
            nets[name] = Net(name=name)
        return nets[name]

    def _add_route(el: SNode) -> None:
        name = _resolve_net_name(_first(el, "net"), num_to_name)
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

    def _add_via(el: SNode) -> None:
        name = _resolve_net_name(_first(el, "net"), num_to_name)
        if not name:
            return
        at = _first(el, "at")
        x, y = _fatom(at, 0), _fatom(at, 1)
        if x is None or y is None:
            return
        layers_node = _first(el, "layers")
        layers = _atoms(layers_node) if layers_node else []
        # KiCad writes the class as a bare atom right after `via`:
        # `(via micro ...)` / `(via blind ...)`; a plain `(via ...)` is through.
        head_atoms = _atoms(el)
        vtype = head_atoms[0] if head_atoms and head_atoms[0] in (
            "micro", "blind", "buried") else "through"
        _ensure(name).vias.append(Via(
            x_mm=x, y_mm=y,
            from_layer=layers[0] if layers else None,
            to_layer=layers[1] if len(layers) > 1 else None,
            via_type=vtype,
            drill_mm=_fatom(_first(el, "drill"), 0),
        ))

    def _add_zone(el: SNode) -> None:
        name = _resolve_net_name(_first(el, "net"), num_to_name)
        if not name:
            return
        for fp in _tagged(el, "filled_polygon"):
            layer_node = _first(fp, "layer")
            layer = _atoms(layer_node)[0] if layer_node and _atoms(layer_node) else None
            pts_node = _first(fp, "pts")
            if pts_node is None:
                continue
            pts = [(x, y) for xy in _tagged(pts_node, "xy")
                   for x in [_fatom(xy, 0)] for y in [_fatom(xy, 1)]
                   if x is not None and y is not None]
            if len(pts) >= 3:
                _ensure(name).fill_regions.append((layer, pts))

    for seg_el in _tagged(root, "segment"):
        _add_route(seg_el)
    for arc_el in _tagged(root, "arc"):
        _add_route(arc_el)  # chord approximation via start/end
    for via_el in _tagged(root, "via"):
        _add_via(via_el)
    for zone_el in _tagged(root, "zone"):
        _add_zone(zone_el)

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


def _footprint_placement(fp: SNode) -> Tuple[Optional[float], Optional[float], float]:
    """Footprint origin + rotation, across format versions.

    KiCad <=8 writes ``(at X Y [rot])`` on the footprint; KiCad 10.99
    (20260623) replaced it with a ``(transform (translate X Y) (rotate R))``
    block. Fall back to the transform when ``(at ...)`` is absent.
    """
    at = _first(fp, "at")
    x, y = _fatom(at, 0), _fatom(at, 1)
    if x is not None and y is not None:
        return x, y, (_fatom(at, 2) or 0.0)
    tr = _first(fp, "transform")
    if tr is not None:
        translate = _first(tr, "translate")
        rotate = _first(tr, "rotate")
        return _fatom(translate, 0), _fatom(translate, 1), (_fatom(rotate, 0) or 0.0)
    return None, None, 0.0


def _convex_hull(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Convex hull (monotone chain) of a point set, CCW, no repeated endpoint."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _courtyard_hull(fp: SNode, ox: float, oy: float, rot_deg: float
                    ) -> Optional[List[Tuple[float, float]]]:
    """Absolute convex-hull outline of a footprint's courtyard graphics
    (``*.CrtYd``).

    Collects the corner points of the courtyard's lines / rects / circles / polys
    in footprint-local space, rotates + translates them to the board, and returns
    their convex hull. A polygon (not a bbox) so a rotated part's keep-out stays
    tight -- a 45 deg rectangle's hull is the rotated rectangle, not its inflated
    box. In the KiCad frame (Y flipped to Gerber later, with pads).
    """
    a = math.radians(rot_deg or 0.0)
    ca, sa = math.cos(a), math.sin(a)
    xs: List[float] = []
    ys: List[float] = []

    def add(lx: float, ly: float) -> None:
        xs.append(ox + lx * ca - ly * sa)
        ys.append(oy + lx * sa + ly * ca)

    def on_crtyd(g: SNode) -> bool:
        ln = _first(g, "layer")
        return bool(ln and _atoms(ln) and "CrtYd" in _atoms(ln)[0])

    def pt(node: Optional[SNode]) -> Optional[Tuple[float, float]]:
        x, y = _fatom(node, 0), _fatom(node, 1)
        return (x, y) if x is not None and y is not None else None

    for g in _tagged(fp, "fp_line"):
        if not on_crtyd(g):
            continue
        for node in (_first(g, "start"), _first(g, "end")):
            p = pt(node)
            if p:
                add(*p)
    for g in _tagged(fp, "fp_rect"):
        if not on_crtyd(g):
            continue
        s, e = pt(_first(g, "start")), pt(_first(g, "end"))
        if s and e:
            for cx, cy in ((s[0], s[1]), (e[0], s[1]), (e[0], e[1]), (s[0], e[1])):
                add(cx, cy)
    for g in _tagged(fp, "fp_circle"):
        if not on_crtyd(g):
            continue
        c, e = pt(_first(g, "center")), pt(_first(g, "end"))
        if c and e:
            r = math.hypot(e[0] - c[0], e[1] - c[1])
            for dx, dy in ((r, 0.0), (-r, 0.0), (0.0, r), (0.0, -r)):
                add(c[0] + dx, c[1] + dy)
    for g in _tagged(fp, "fp_poly"):
        if not on_crtyd(g):
            continue
        pts = _first(g, "pts")
        if pts:
            for xy in _tagged(pts, "xy"):
                p = pt(xy)
                if p:
                    add(*p)

    if not xs:
        return None
    return _convex_hull(list(zip(xs, ys)))


def _parse_components(root: SNode, nets: Dict[str, Net],
                      num_to_name: Dict[str, str]) -> List[Component]:
    comps: List[Component] = []
    for fp in _tagged(root, "footprint"):
        footprint = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else None
        x, y, rot = _footprint_placement(fp)
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
        pads = (_parse_pads(fp, ref, x, y, rot, nets, num_to_name)
                if (x is not None and y is not None) else [])
        courtyard = (_courtyard_hull(fp, x, y, rot)
                     if (x is not None and y is not None) else None)
        comps.append(Component(
            ref=ref, value=value, footprint=footprint,
            x_mm=x, y_mm=y, rotation_deg=rot, side=_side_of_layer(layer),
            pads=pads, courtyard=courtyard,
        ))
    return comps


def _parse_pads(fp: SNode, ref: str, ox: float, oy: float, rot_deg: float,
                nets: Dict[str, Net], num_to_name: Dict[str, str]) -> List[Pad]:
    """Footprint pads in absolute mm (pad ``at`` is relative to the footprint
    origin and is rotated by the footprint rotation).

    A pad's ``(net ...)`` reference also seeds a net access point (``NetPoint``)
    at the pad's absolute location, so a KiCad board -- like an IPC-D-356 netlist
    -- lets the design-data checks resolve pads to nets by coincidence."""
    a = math.radians(rot_deg or 0.0)
    ca, sa = math.cos(a), math.sin(a)
    pads: List[Pad] = []
    for pad in _tagged(fp, "pad"):
        name = pad[1] if len(pad) > 1 and isinstance(pad[1], str) else None
        if name is None:
            continue
        pad_type = pad[2] if len(pad) > 2 and isinstance(pad[2], str) else None
        at = _first(pad, "at")
        dx, dy = _fatom(at, 0), _fatom(at, 1)
        if dx is None or dy is None:
            continue
        through = pad_type in ("thru_hole", "np_thru_hole")
        ax = ox + dx * ca - dy * sa
        ay = oy + dx * sa + dy * ca
        shape = pad[3] if len(pad) > 3 and isinstance(pad[3], str) else None
        size = _first(pad, "size")
        pw, ph = _fatom(size, 0), _fatom(size, 1)
        pad_rot = _fatom(at, 2) or 0.0   # pad rotation, relative to the footprint
        pads.append(Pad(
            name=name, x_mm=ax, y_mm=ay,
            pad_type=pad_type, through_hole=through,
            width_mm=pw, height_mm=ph, shape=shape,
            rotation_deg=(rot_deg or 0.0) + pad_rot,
        ))
        net_name = _resolve_net_name(_first(pad, "net"), num_to_name)
        if net_name:
            net = nets.get(net_name)
            if net is None:
                net = nets[net_name] = Net(name=net_name)
            net.points.append(NetPoint(
                x_mm=ax, y_mm=ay,
                kind="through" if through else "smd",
                ref=ref, pin=name,
            ))
    return pads


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _to_gerber_frame(components: List[Component], nets: Dict[str, Net]) -> None:
    """Flip extracted Y into the artwork (Gerber) frame, in place.

    KiCad stores Y increasing *downward*; Gerber -- and gerbonara's native render
    of a KiCad board -- is Y-up, which is a pure negation of Y about the origin
    (verified: rendered copper/drills land at exactly -y of the design records).
    Flipping the extracted coordinates puts pads over their copper and net points
    inside their mask openings, so the design-data checks correlate with the
    artwork instead of sitting mirrored ~2x the board height away.

    Distance/topology checks that read only design data (pad<->net resolution,
    decoupling proximity) are invariant under the flip; the artwork-correlating
    ones (test-point coverage, footprint-aware mask/spacing) require it.
    """
    for c in components:
        if c.y_mm is not None:
            c.y_mm = -c.y_mm
        for p in c.pads:
            p.y_mm = -p.y_mm
            p.rotation_deg = -p.rotation_deg   # mirror about X flips orientation
        if c.courtyard is not None:
            c.courtyard = [(px, -py) for (px, py) in c.courtyard]  # flip Y
    for net in nets.values():
        for pt in net.points:
            pt.y_mm = -pt.y_mm
        for v in net.vias:
            v.y_mm = -v.y_mm
        for f in net.features:
            if f.segments:
                f.segments = [((x0, -y0), (x1, -y1))
                              for ((x0, y0), (x1, y1)) in f.segments]
        net.fill_regions = [(layer, [(x, -y) for (x, y) in pts])
                            for (layer, pts) in net.fill_regions]


def from_kicad(source: Union[str, Path]) -> DesignData:
    board, pro = _resolve_board(Path(source))
    root = _parse_sexpr(board.read_text(encoding="utf-8"))

    num_to_name = _net_number_table(root)
    nets = _parse_nets_and_routes(root, num_to_name)
    components = _parse_components(root, nets, num_to_name)
    _apply_board_netclasses(root, nets)
    if pro is not None:
        _apply_project_netclasses(pro, nets)
    _to_gerber_frame(components, nets)

    return DesignData(
        stackup=_parse_stackup(root),
        nets=nets,
        diff_pairs=_infer_diff_pairs(nets),
        components=components,
        source="kicad",
    )
