"""Render a ``.kicad_pcb`` to artwork natively, with no KiCad install (#13 Tier 3).

Tier 2 shells out to ``kicad-cli``; this path needs nothing installed. It drives
gerbonara's KiCad model, which already maintains the ``.kicad_pcb`` schema
mapping, and renders the board into a gerbonara ``LayerStack`` -- the very
primitives our Gerber backend consumes. Writing that stack out gives a normal
Gerber + Excellon package, so the **entire** ruleset runs, not a subset that
happens to work off ``BoardGeometry`` alone.

The zone-fill hazard
--------------------
This is the reason the native path was deferred, and it is handled here rather
than hoped away. Poured copper lives in a ``.kicad_pcb`` only as
``filled_polygon`` records, written when the user last refilled zones. Edit a
board and save without refilling and the file still holds the zone *outline*
while the actual copper is stale or absent -- so a renderer that trusts it would
measure copper that differs from what gets fabricated, silently.

:func:`zone_fill_state` reports that up front, and :func:`render_to_gerber_zip`
refuses by default when zones are unfilled. Measuring the wrong geometry quietly
is worse than declining loudly.

What this cannot tell you
-------------------------
Rendering from a design file answers "is this design manufacturable", not "is
this fabrication package correct". Export-time faults -- wrong plot settings, a
missing layer, a scaling mistake -- exist only in the package a user actually
sends, and artwork rendered here by construction cannot contain them. Results
carry ``geometry_source="kicad-native"`` so a report can never imply otherwise.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

GEOMETRY_SOURCE_KICAD_NATIVE = "kicad-native"


@dataclass
class ZoneFillState:
    """Whether a board's copper pours are actually present in the file."""
    total: int = 0
    filled: int = 0

    @property
    def unfilled(self) -> int:
        return self.total - self.filled

    @property
    def ok(self) -> bool:
        """True when every zone carries poured copper (or there are no zones)."""
        return self.unfilled == 0

    def describe(self) -> str:
        if self.total == 0:
            return "no copper zones on this board"
        if self.ok:
            return f"all {self.total} copper zone(s) are filled"
        return (
            f"{self.unfilled} of {self.total} copper zone(s) have no poured copper "
            f"in the file -- refill zones in KiCad (Edit > Fill All Zones) and save"
        )


def _load_board(source: Union[str, Path]):
    from gerbonara.cad.kicad.pcb import Board

    path = Path(source)
    if path.is_dir():
        pcbs = sorted(path.glob("*.kicad_pcb"))
        if not pcbs:
            raise ValueError(f"no .kicad_pcb found in project directory: {path}")
        path = pcbs[0]
    elif path.suffix.lower() == ".kicad_pro":
        sibling = path.with_suffix(".kicad_pcb")
        if not sibling.is_file():
            raise ValueError(f"no .kicad_pcb beside project file: {path}")
        path = sibling
    return Board.open(str(path)), path


def zone_fill_state(source: Union[str, Path]) -> ZoneFillState:
    """Report how many copper zones actually carry poured copper.

    A zone is considered filled when the file holds ``filled_polygon`` geometry
    for it. Keepout zones are ignored: they define a rule, not copper.
    """
    board, _path = _load_board(source)
    state = ZoneFillState()
    for zone in getattr(board, "zones", []) or []:
        if getattr(zone, "keepout", None) is not None:
            continue
        state.total += 1
        fills = getattr(zone, "filled_polygons", None)
        if fills is None:
            fills = getattr(zone, "fill_polygons", None)
        if fills:
            state.filled += 1
    return state


def render_to_gerber_zip(
    source: Union[str, Path],
    out_zip: Optional[Path] = None,
    *,
    allow_unfilled_zones: bool = False,
) -> Path:
    """Render a KiCad board to a Gerber + Excellon zip the normal ingest can read.

    Raises ``RuntimeError`` when zones are unfilled unless
    ``allow_unfilled_zones`` is set, because the poured copper simply is not in
    the file and any geometry measured without it would be wrong rather than
    merely incomplete.
    """
    from gerbonara.cad.kicad.base_types import LAYER_MAP_K2G
    from gerbonara.layers import LayerStack, NamingScheme

    state = zone_fill_state(source)
    if not state.ok and not allow_unfilled_zones:
        raise RuntimeError(
            f"cannot render this board faithfully: {state.describe()}. "
            f"Copper pours are absent from the file, so measured geometry would "
            f"not match the fabricated board."
        )

    board, board_path = _load_board(source)
    stack = LayerStack()
    layer_map = {kc: gn for kc, gn in LAYER_MAP_K2G.items() if gn in stack}

    # gerbonara 1.5's Board.render() dispatches every object through
    # obj.render(variables=...), but Footprint.render() takes (layer_stack,
    # layer_map) -- so it raises TypeError on any board with footprints, i.e.
    # every real board. Drive the pieces ourselves instead, mirroring what that
    # method does for the cases it gets right.
    from gerbonara.cad.kicad.footprints import Footprint
    from gerbonara.cad.kicad.pcb import Zone
    from gerbonara.graphic_objects import Region
    from gerbonara.utils import MM

    for obj in board.objects(images=False, vias=False, text=False):
        if isinstance(obj, Footprint):
            # gerbonara 1.5's Footprint.render does NOT mirror y, while every
            # other object's render does (KiCad's y grows downward, Gerber's
            # upward). Left alone, a footprint at y=15 lands at +15 while the
            # board it belongs to is at -15 -- pads 30 mm off the board, on the
            # wrong side of it, with no error raised.
            #
            # Its own maths is `y = param_y + at.y` applied to a sub-object that
            # HAS been mirrored, giving `at.y - local_y` where `-at.y - local_y`
            # is wanted. Passing -2*at.y corrects it exactly; the rotation needs
            # the matching negation because mirroring reverses handedness.
            at = getattr(obj, "at", None)
            fy = float(getattr(at, "y", 0.0) or 0.0)
            frot = float(getattr(at, "rotation", 0.0) or 0.0)
            obj.render(stack, layer_map,
                       y=-2.0 * fy, rotation=-2.0 * math.radians(frot))
            continue

        if isinstance(obj, Zone):
            # Zones carry no render() in gerbonara 1.5. Emit the POURED copper
            # (fill_polygons), never the zone outline: the outline is the region
            # the user asked to be filled, not the copper that results from it,
            # and treating one as the other is exactly the staleness this module
            # guards against.
            for fill in getattr(obj, "fill_polygons", None) or []:
                pts = [(pt.x, pt.y) for pt in (fill.pts or [])]
                if len(pts) < 3:
                    continue
                layer = layer_map.get(getattr(fill, "layer", None) or obj.layer)
                if not layer:
                    continue
                stack[layer].objects.append(
                    Region([(x, -y) for (x, y) in pts], unit=MM)
                )
            continue

        layer = layer_map.get(getattr(obj, "layer", None))
        if not layer:
            continue
        for fe in obj.render():
            stack[layer].objects.append(fe)

    for via in board.vias:
        import fnmatch

        for glob in via.layers or []:
            for kc_layer in fnmatch.filter(layer_map, glob):
                for fe in via.render():
                    stack[layer_map[kc_layer]].objects.append(fe)
        for fe in via.render_drill():
            stack.drill_pth.append(fe)

    out_zip = Path(out_zip) if out_zip else (
        Path(tempfile.mkdtemp(prefix="pcb_dfm_kicad_native_")) / f"{board_path.stem}.zip"
    )
    # Write with conventional extensions (.gtl/.gts/.gbl...) rather than
    # KiCad's own "-F.Cu.gbr" names: the ingest classifies layers by those, and
    # a package it cannot classify would silently run almost no checks.
    stack.save_to_zipfile(
        out_zip, board_name=board_path.stem, naming_scheme=NamingScheme.altium,
    )
    return out_zip


def rendered_layer_summary(source: Union[str, Path]) -> List[str]:
    """Names of the layers that came out non-empty. Useful for diagnostics."""
    zip_path = render_to_gerber_zip(source, allow_unfilled_zones=True)
    import zipfile

    with zipfile.ZipFile(zip_path) as zf:
        return sorted(zf.namelist())
