"""Shared resolver for electrical design-review checks.

Turns raw ``DesignData`` into the resolved electrical picture the review checks
ask about -- pins per net, net functions, part classes, and the "is there a cap
from this rail to ground?" style predicates -- computed once via
``design_intel.PadNetIndex`` so no check re-derives it.

Follows the Tier-2 / review contract: everything is grounded in resolved facts,
and ``resolve_design`` returns None (⇒ the check is not_applicable) when there is
no netlist + BOM to resolve, rather than guessing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from ..ingest.design_intel import (
    PadNetIndex,
    build_pad_net_index,
    classify_component,
    classify_net,
)
from ..ingest.design_model import Component, DesignData

_CAP_RE = re.compile(r"([\d.]+)\s*(p|n|u|µ|micro|m)?f?", re.I)
_CAP_UNIT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "micro": 1e-6, "m": 1e-3, "": 1e-6}


def cap_farads(comp: Component) -> Optional[float]:
    """Capacitance in farads parsed from a capacitor's value, or None if unknown."""
    val = (comp.value or "").lower().replace(" ", "")
    m = _CAP_RE.match(val)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    return num * _CAP_UNIT[(m.group(2) or "").lower()]


@dataclass
class ResolvedDesign:
    dd: DesignData
    idx: PadNetIndex
    net_func: Dict[str, str]                 # net -> "power" | "ground" | "signal"
    part_class: Dict[str, Optional[str]]     # ref -> class
    comp_by_ref: Dict[str, Component]
    pins_on_net: Dict[str, int] = field(default_factory=dict)

    # -- net-function sets --------------------------------------------------
    def ground_nets(self) -> Set[str]:
        return {n for n, f in self.net_func.items() if f == "ground"}

    def power_nets(self) -> Set[str]:
        return {n for n, f in self.net_func.items() if f == "power"}

    # -- adjacency ----------------------------------------------------------
    def nets_of(self, ref: str) -> Set[str]:
        return self.idx.nets_of(ref)

    def comps_on(self, net: str) -> Set[str]:
        return self.idx.components_on(net)

    def classes_on(self, net: str) -> Set[Optional[str]]:
        return {self.part_class.get(r) for r in self.comps_on(net)}

    # -- component role helpers --------------------------------------------
    def refs_of_class(self, cls: str) -> List[str]:
        return [r for r, c in self.part_class.items() if c == cls]

    def ic_supply_rails(self) -> Dict[str, int]:
        """Power nets that reach an IC pad -> IC-pad count on that rail."""
        pins: Dict[str, int] = defaultdict(int)
        for (ref, _pad), net in self.idx.pad_net.items():
            if self.part_class.get(ref) == "ic" and self.net_func.get(net) == "power":
                pins[net] += 1
        return dict(pins)

    # -- "is there a <part> bridging this net to a <function> rail?" --------
    def _bridges(self, ref: str, net: str, other_func: str) -> bool:
        nets = self.nets_of(ref)
        return net in nets and any(self.net_func.get(n) == other_func for n in nets)

    def cap_to_ground_on(self, net: str) -> bool:
        """Any capacitor bridging ``net`` to a ground net (bypass/​load cap)."""
        return any(
            self.part_class.get(r) == "capacitor" and self._bridges(r, net, "ground")
            for r in self.comps_on(net))

    def resistor_to_power_on(self, net: str) -> bool:
        """Any resistor bridging ``net`` to a power net (a pull-up)."""
        return any(
            self.part_class.get(r) == "resistor" and self._bridges(r, net, "power")
            for r in self.comps_on(net))

    def has_class_on(self, net: str, cls: str) -> bool:
        return any(self.part_class.get(r) == cls for r in self.comps_on(net))

    def pads_on(self, net: str) -> list:
        """The Pad objects (with geometry) resolved onto ``net``."""
        out = []
        for (ref, padname), n in self.idx.pad_net.items():
            if n != net:
                continue
            comp = self.comp_by_ref.get(ref)
            if comp is None:
                continue
            for p in comp.pads:
                if p.name == padname:
                    out.append(p)
                    break
        return out

    def pad_points_on(self, net: str) -> List[tuple]:
        """(x, y) of every component pad resolved onto ``net``."""
        out = []
        for (ref, padname), n in self.idx.pad_net.items():
            if n != net:
                continue
            comp = self.comp_by_ref.get(ref)
            if comp is None:
                continue
            for p in comp.pads:
                if p.name == padname and p.x_mm is not None:
                    out.append((p.x_mm, p.y_mm))
                    break
        return out


def resolve_design(dd: Optional[DesignData]) -> Optional[ResolvedDesign]:
    """Resolve ``dd`` into a ``ResolvedDesign``, or None when there isn't enough
    (no design data, no nets/components, or no pad↔net matches) to review."""
    if dd is None or not dd.nets or not dd.components:
        return None
    idx = build_pad_net_index(dd)
    if not idx.pad_net:
        return None
    net_func = {name: classify_net(name, net.net_class) for name, net in dd.nets.items()}
    comp_by_ref = {c.ref: c for c in dd.components}
    part_class = {ref: classify_component(c)[0] for ref, c in comp_by_ref.items()}
    pins_on_net: Dict[str, int] = defaultdict(int)
    for (_ref, _pad), net in idx.pad_net.items():
        pins_on_net[net] += 1
    return ResolvedDesign(
        dd=dd, idx=idx, net_func=net_func, part_class=part_class,
        comp_by_ref=comp_by_ref, pins_on_net=dict(pins_on_net))
