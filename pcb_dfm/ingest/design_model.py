"""
Format-agnostic design-data model.

Bare Gerbers carry no connectivity or layer-stack information. This module
defines the *internal* representation that DFM checks consume, independent of
where it came from. Concrete inputs are mapped onto this model by adapters in
``pcb_dfm.ingest.adapters`` (a JSON sidecar today, IPC-2581 now, ODB++ later).

Keeping checks coupled only to this model means a new input format is a new
adapter, not a change to every check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# A routed segment in board mm-space: ((x0, y0), (x1, y1)).
Point = Tuple[float, float]
Segment = Tuple[Point, Point]


@dataclass
class StackupLayer:
    """One physical layer in the board stack."""
    name: str
    kind: str  # "copper" | "dielectric"
    thickness_mm: Optional[float] = None
    er: Optional[float] = None  # dielectric constant (dielectric layers only)


@dataclass
class Stackup:
    """Ordered board stackup (top -> bottom)."""
    layers: List[StackupLayer] = field(default_factory=list)

    def dielectric_layers(self) -> List[StackupLayer]:
        return [ly for ly in self.layers if ly.kind == "dielectric"]

    def copper_layers(self) -> List[StackupLayer]:
        return [ly for ly in self.layers if ly.kind == "copper"]

    def dielectric_thicknesses_mm(self) -> List[float]:
        return [ly.thickness_mm for ly in self.dielectric_layers()
                if ly.thickness_mm is not None]

    @property
    def er(self) -> Optional[float]:
        """Representative dielectric constant (first dielectric with an Er)."""
        for ly in self.dielectric_layers():
            if ly.er is not None:
                return ly.er
        return None

    @property
    def dielectric_thickness_mm(self) -> Optional[float]:
        """Representative dielectric height (first dielectric with a thickness)."""
        for ly in self.dielectric_layers():
            if ly.thickness_mm is not None:
                return ly.thickness_mm
        return None

    @property
    def copper_thickness_mm(self) -> Optional[float]:
        """Representative finished copper thickness (first copper layer)."""
        for ly in self.copper_layers():
            if ly.thickness_mm is not None:
                return ly.thickness_mm
        return None

    def total_thickness_mm(self) -> Optional[float]:
        """Total finished board thickness = sum of every layer thickness.

        Sums ``thickness_mm`` across all copper *and* dielectric layers in the
        stack. Returns None when no layer carries a thickness (so callers can
        fall back to a default).
        """
        thicknesses = [ly.thickness_mm for ly in self.layers
                       if ly.thickness_mm is not None]
        if not thicknesses:
            return None
        total = sum(thicknesses)
        return total if total > 0 else None


@dataclass
class NetFeature:
    """A routed feature belonging to a net.

    ``length_mm`` is always populated (summed by the adapter); ``segments`` and
    ``width_mm`` carry the actual routed geometry when the source provides it
    (e.g. IPC-2581 <Line>/<Arc>), enabling geometry-aware checks like diff-pair
    spacing. When only a length is known, ``segments`` is empty.
    """
    layer: Optional[str] = None
    length_mm: float = 0.0
    width_mm: Optional[float] = None
    segments: List[Segment] = field(default_factory=list)


@dataclass
class Via:
    """A layer-transition via on a net (routing topology)."""
    x_mm: float
    y_mm: float
    from_layer: Optional[str] = None
    to_layer: Optional[str] = None


@dataclass
class NetPoint:
    """A net access point in absolute board mm-space.

    This is what a NETLIST gives you (IPC-D-356 and friends): the location of a
    pad, pin or via together with the net it belongs to. Unlike ``NetFeature``
    it carries no routed path -- just "this point is on this net" -- which is
    still enough to label copper by net, because any copper polygon containing
    the point is on that net.
    """
    x_mm: float
    y_mm: float
    kind: str = "through"          # "through" (via/THT pin) | "smd"
    ref: Optional[str] = None      # component reference designator, or "VIA"
    pin: Optional[str] = None
    layer: Optional[str] = None    # None = all layers (through-hole)


@dataclass
class Net:
    name: str
    features: List[NetFeature] = field(default_factory=list)
    net_class: Optional[str] = None
    vias: List[Via] = field(default_factory=list)
    points: List[NetPoint] = field(default_factory=list)

    def routed_length_mm(self) -> float:
        return sum(f.length_mm for f in self.features)

    def route_segments(self) -> List[Tuple[Segment, Optional[str], Optional[float]]]:
        """All routed segments as (segment, layer, width_mm) across features."""
        out: List[Tuple[Segment, Optional[str], Optional[float]]] = []
        for f in self.features:
            for seg in f.segments:
                out.append((seg, f.layer, f.width_mm))
        return out

    def has_geometry(self) -> bool:
        return any(f.segments for f in self.features)

    def has_points(self) -> bool:
        """True when a netlist supplied access points for this net."""
        return bool(self.points)


@dataclass
class DiffPair:
    """A differential pair identified by its two member net names."""
    name: str
    positive: str
    negative: str
    target_ohm: Optional[float] = None


@dataclass
class ControlledImpedanceSpec:
    """A controlled-impedance constraint on a net or net class."""
    name: str
    target_ohm: float
    width_mm: Optional[float] = None
    tolerance_pct: float = 10.0


@dataclass
class Pad:
    """A component pad in absolute board mm-space.

    ``name`` is the pad / pin identifier from the footprint ("1", "2", "A1",
    "K", ...). ``through_hole`` distinguishes wave-soldered THT pads from SMD.
    """
    name: str
    x_mm: float
    y_mm: float
    pad_type: Optional[str] = None  # "smd" | "thru_hole" | "np_thru_hole" | ...
    through_hole: bool = False


@dataclass
class Component:
    """A placed component, optionally enriched with BOM identity.

    Placement (footprint) fields carry geometry; the BOM-derived fields below
    carry identity. A component may be geometry-only (placed, no BOM row),
    identity-only (in the BOM but not laid out -> ``placed=False``), or both.
    """
    ref: str
    value: Optional[str] = None
    footprint: Optional[str] = None
    x_mm: Optional[float] = None
    y_mm: Optional[float] = None
    rotation_deg: float = 0.0
    side: Optional[str] = None  # "top" | "bottom"
    # --- BOM-derived identity (#6) ---
    part_number: Optional[str] = None       # manufacturer part number (MPN)
    manufacturer: Optional[str] = None
    description: Optional[str] = None
    part_class: Optional[str] = None        # resistor|capacitor|...|other
    polarized: Optional[bool] = None        # None = unknown
    dnp: bool = False                       # do-not-populate
    height_mm: Optional[float] = None       # body height, when the BOM carries it
    placed: bool = True                     # False = in the BOM but not laid out
    pads: List["Pad"] = field(default_factory=list)  # absolute pad geometry (#6)

    def pin1(self) -> Optional["Pad"]:
        """The pin-1 pad, if identifiable."""
        for p in self.pads:
            if p.name == "1":
                return p
        return None


@dataclass
class DesignData:
    """Everything derived from a design-data source, in one place."""
    stackup: Optional[Stackup] = None
    nets: Dict[str, Net] = field(default_factory=dict)
    diff_pairs: List[DiffPair] = field(default_factory=list)
    controlled_impedance: List[ControlledImpedanceSpec] = field(default_factory=list)
    components: List[Component] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)  # non-fatal ingest/merge notes
    source: Optional[str] = None  # "sidecar" | "ipc2581" | "odbpp" | "kicad"

    def net(self, name: str) -> Optional[Net]:
        return self.nets.get(name)

    def add_net(self, net: Net) -> None:
        self.nets[net.name] = net
