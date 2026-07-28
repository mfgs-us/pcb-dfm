"""Design-data intelligence helpers shared by the Tier-2 design checks.

These turn the raw ingested ``DesignData`` into the questions the checks actually
ask -- "what function is this net?", "what kind of part is this?", "which net is
this pad on?" -- without any one check re-deriving them. They are pure functions
over ``DesignData`` (no geometry, no I/O), so they are cheap and testable.

Nothing here fabricates data: when a field is absent the helpers return the
"unknown" answer, so a check built on them stays ``not_applicable`` rather than
guessing. That is the Tier-2 contract.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .design_model import Component, DesignData

# --------------------------------------------------------------------------
# E3 -- net function classifier (power / ground / signal)
# --------------------------------------------------------------------------

_GROUND_TOKENS = ("gnd", "vss", "agnd", "dgnd", "pgnd", "gndp", "earth", "vssa")
_POWER_TOKENS = (
    "vcc", "vdd", "vbat", "vin", "vbus", "vsys", "vpp", "vddio", "vdda",
    "vref", "vaa", "avdd", "dvdd", "vcore", "vio", "3v3", "5v", "1v8", "1v2",
    "2v5", "3v0", "12v", "vddq",
)
# A leading rail like +3V3 / -12V / 5V0.
_RAIL_RE = re.compile(r"^[+-]?\d+v\d*$")

NetFunction = str  # "power" | "ground" | "signal"


def classify_net(name: Optional[str], net_class: Optional[str] = None) -> NetFunction:
    """Best-effort net function from its name (and net_class hint).

    Name-based, so it is a heuristic -- but power/ground rails are named by strong
    convention, and mis-labelling a signal as "signal" (the default) is harmless.
    """
    n = (name or "").strip().lower()
    nc = (net_class or "").strip().lower()
    if not n:
        return "signal"
    # The NAME's rail token wins over the net class: KiCad commonly puts GND *and*
    # VCC in one "Power" net class for width rules, so honouring the class first
    # would mis-label GND as power. A trailing index (GND1, VCC2) is stripped so
    # the keyword still matches; ground first (VSS/GND are unambiguous).
    tokens = re.split(r"[^a-z0-9]+", n)
    stems = {re.sub(r"\d+$", "", t) or t for t in tokens} | set(tokens)
    if stems & set(_GROUND_TOKENS):
        return "ground"
    if stems & set(_POWER_TOKENS) or any(_RAIL_RE.match(t) for t in tokens):
        return "power"
    # Otherwise fall back to an explicit net-class hint.
    if "ground" in nc or nc in ("gnd", "power_gnd"):
        return "ground"
    if "power" in nc or nc in ("pwr", "supply"):
        return "power"
    return "signal"


def is_power_or_ground(name: Optional[str], net_class: Optional[str] = None) -> bool:
    return classify_net(name, net_class) in ("power", "ground")


_GND_HINTS = ("gnd", "vss", "neg", "return", "_ret", "agnd", "dgnd", "pgnd", "earth")


def net_function_with_pins(name: Optional[str], net_class: Optional[str],
                           pin_types: "set[str] | frozenset[str] | tuple") -> NetFunction:
    """Net function, refined by schematic pin electrical types.

    Name/net_class classification wins when it is decisive. Otherwise a net that
    feeds a ``power_in``/``power_out`` pin is a supply rail -- functional evidence
    a name can't give (e.g. a battery IC's ``BAT_NEG`` reference, which no rail
    token matches). This is how schematic ingest sharpens the checks that key off
    net function (decoupling, floating, coupled-run, ...).
    """
    base = classify_net(name, net_class)
    if base != "signal":
        return base
    if any(t in ("power_in", "power_out") for t in pin_types):
        n = (name or "").lower()
        return "ground" if any(h in n for h in _GND_HINTS) else "power"
    return "signal"


# --------------------------------------------------------------------------
# E4 -- component classifier (part class + polarity)
# --------------------------------------------------------------------------

# Reference-designator prefixes are the most reliable part-class signal, more so
# than free-text values. Longest prefix wins (LED before L, FB before F).
_REF_PREFIX_CLASS = [
    ("LED", "led"), ("FB", "ferrite"), ("TP", "testpoint"), ("MH", "mounting"),
    ("FID", "fiducial"), ("XTAL", "crystal"),
    ("R", "resistor"), ("C", "capacitor"), ("L", "inductor"), ("D", "diode"),
    ("U", "ic"), ("Q", "transistor"), ("Y", "crystal"), ("X", "crystal"),
    ("J", "connector"), ("P", "connector"), ("SW", "switch"), ("K", "relay"),
    ("F", "fuse"), ("T", "transformer"), ("BT", "battery"), ("AE", "antenna"),
    ("ANT", "antenna"),
]

_POLARIZED_CLASSES = {"led", "diode", "battery", "transistor", "electrolytic"}


def _class_from_ref(ref: Optional[str]) -> Optional[str]:
    if not ref:
        return None
    r = ref.strip().upper()
    for prefix, cls in _REF_PREFIX_CLASS:
        if r.startswith(prefix) and (len(r) == len(prefix) or r[len(prefix)].isdigit()):
            return cls
    return None


def classify_component(comp: Component) -> Tuple[Optional[str], Optional[bool]]:
    """Return ``(part_class, polarized)`` for a component.

    Prefers any class the adapter already resolved (``comp.part_class`` from a
    BOM), else infers from the reference-designator prefix, else the footprint.
    ``polarized`` is True for parts whose orientation matters for assembly
    (diodes, LEDs, electrolytic caps, ...), None when unknown.
    """
    cls = (comp.part_class or "").strip().lower() or _class_from_ref(comp.ref)
    fp = (comp.footprint or "").lower()
    val = (comp.value or "").lower()

    if cls is None:
        if "cap" in fp:
            cls = "capacitor"
        elif "resistor" in fp or "_r_" in fp:
            cls = "resistor"
        elif "led" in fp:
            cls = "led"
        elif "diode" in fp:
            cls = "diode"

    # A D-prefixed part classifies as a generic diode from its refdes, but an LED
    # footprint (LED-SMD, ...) is decisive -- refine so LED-aware checks (series
    # resistor, indicator-sink pull-up) recognise it.
    if cls == "diode" and "led" in fp:
        cls = "led"

    polarized: Optional[bool] = comp.polarized
    if polarized is None and cls is not None:
        if cls in _POLARIZED_CLASSES:
            polarized = True
        elif cls == "capacitor":
            # Electrolytic / tantalum caps are polarized; ceramics are not.
            polarized = any(k in fp or k in val for k in ("elec", "tant", "cp_", "polar"))
        elif cls in ("resistor", "inductor", "ferrite", "crystal", "fuse"):
            polarized = False
    return cls, polarized


def is_decoupling_candidate(comp: Component) -> bool:
    """A small ceramic capacitor that could be a decoupling cap (value <= 1 uF)."""
    cls, _ = classify_component(comp)
    if cls != "capacitor":
        return False
    val = (comp.value or "").lower().replace(" ", "")
    m = re.match(r"([\d.]+)\s*(p|n|u|µ|micro)?f?", val)
    if not m:
        return True  # unknown value -> keep as a candidate rather than exclude
    num = float(m.group(1))
    unit = m.group(2) or ""
    farads = num * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "micro": 1e-6, "": 1e-6}[unit]
    return farads <= 1e-6


# --------------------------------------------------------------------------
# E2 -- pad <-> net <-> component resolver
# --------------------------------------------------------------------------


class PadNetIndex:
    """Resolves component pads to nets and back, by matching pad geometry to net
    access points. Built from ``DesignData`` alone -- both carry absolute mm
    coordinates, so no artwork is needed."""

    def __init__(self) -> None:
        self.pad_net: Dict[Tuple[str, str], str] = {}         # (ref, pad_name) -> net
        self.net_components: Dict[str, set] = {}              # net -> {ref}
        self.component_nets: Dict[str, set] = {}              # ref -> {net}
        self.unmatched_pads: List[Tuple[str, str]] = []       # pads that hit no net

    def nets_of(self, ref: str) -> set:
        return self.component_nets.get(ref, set())

    def components_on(self, net: str) -> set:
        return self.net_components.get(net, set())


def build_pad_net_index(dd: DesignData, tol_mm: float = 0.05) -> PadNetIndex:
    """Match each component pad to the net whose access point coincides with it."""
    idx = PadNetIndex()
    # Bucket net access points into a coarse grid for a fast nearest lookup.
    cell = max(tol_mm, 0.05)
    grid: Dict[Tuple[int, int], List[Tuple[float, float, str]]] = {}
    for net in dd.nets.values():
        for pt in net.points or []:
            gkey = (int(pt.x_mm / cell), int(pt.y_mm / cell))
            grid.setdefault(gkey, []).append((pt.x_mm, pt.y_mm, net.name))

    for comp in dd.components:
        for pad in comp.pads:
            best_net: Optional[str] = None
            best_d2 = tol_mm * tol_mm
            gx, gy = int(pad.x_mm / cell), int(pad.y_mm / cell)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for (px, py, nm) in grid.get((gx + di, gy + dj), ()):
                        d2 = (px - pad.x_mm) ** 2 + (py - pad.y_mm) ** 2
                        if d2 <= best_d2:
                            best_d2 = d2
                            best_net = nm
            key = (comp.ref, pad.name)
            if best_net is not None:
                idx.pad_net[key] = best_net
                idx.net_components.setdefault(best_net, set()).add(comp.ref)
                idx.component_nets.setdefault(comp.ref, set()).add(best_net)
            else:
                idx.unmatched_pads.append(key)
    return idx
