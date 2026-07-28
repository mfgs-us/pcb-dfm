"""Whole-board fixtures for the design-review trust corpus (#9).

The Tier-2 design-review checks (``category_id == "design_advisory"``) were
hardened against a class of real-board false positives in the #85 pass -- GND
landing in a "Power" net class, a battery IC referenced only by ``BAT_NEG``,
thin stitching runs on a poured plane read as a power neck-down, and so on. Those
bugs lived in the check + ``design_intel`` layer, not the KiCad parser, and none
of them were caught by the gerber-only golden corpus (which has no netlist, so
every design-review check is N/A there).

These fixtures are *whole boards*: a realistic MCU core with the exact patterns
that tripped us, built so the entire design-review suite runs at once and its
cross-check interactions are exercised. Each board declares ``must_pass`` -- the
checks that a correct engine keeps clean on it -- and the trust test also diffs a
full per-check digest, so a regression that flips any status (a returning FP, or
a lost finding) fails CI.

The builder mirrors ``tests/test_design_review_electrical.py``: a net access
point is dropped at every pad so ``PadNetIndex`` resolves pad->net by geometry.
Pads are given real footprint geometry and nets carry net_class / poured
fill-regions / stitching vias so the geometry-aware guards are covered too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pcb_dfm.ingest.design_model import (
    Component,
    DesignData,
    Net,
    NetFeature,
    NetPoint,
    Pad,
    Via,
)

# (pin_name, x_mm, y_mm, net_name)
PadSpec = Tuple[str, float, float, str]


@dataclass
class CompSpec:
    ref: str
    value: Optional[str]
    pads: List[PadSpec]
    footprint: Optional[str] = None
    part_class: Optional[str] = None


@dataclass
class NetMeta:
    net_class: Optional[str] = None
    poured: bool = False                       # -> a fill region + stitching vias
    vias: List[Tuple[float, float]] = field(default_factory=list)
    # Routed copper as (x0, y0, x1, y1, width_mm); one NetFeature per segment.
    segments: List[Tuple[float, float, float, float, float]] = field(default_factory=list)


@dataclass
class BoardSpec:
    name: str
    description: str
    comps: List[CompSpec]
    net_meta: Dict[str, NetMeta] = field(default_factory=dict)
    # (ref, pin) -> electrical type ("power_in" | "output" | "bidirectional" | ...)
    pin_types: Dict[Tuple[str, str], str] = field(default_factory=dict)
    nc_pins: List[Tuple[str, str]] = field(default_factory=list)
    # Checks a correct engine MUST keep clean (status "pass") on this board.
    # Each entry documents a real FP class the #85 pass fixed.
    must_pass: Dict[str, str] = field(default_factory=dict)

    def build(self, offset: Tuple[float, float] = (0.0, 0.0)) -> DesignData:
        """Build the DesignData, optionally translating every coordinate by
        ``offset`` -- used by the translation-invariance property test, since a
        correct design-review verdict is independent of absolute board position."""
        ox, oy = offset
        dd = DesignData(source="trust")
        nets: Dict[str, Net] = {}
        comps: List[Component] = []
        for cs in self.comps:
            pads = []
            for (pin, x, y, net) in cs.pads:
                pads.append(Pad(name=pin, x_mm=x + ox, y_mm=y + oy, width_mm=0.6,
                                height_mm=0.6, shape="roundrect"))
                nets.setdefault(net, Net(name=net)).points.append(
                    NetPoint(x_mm=x + ox, y_mm=y + oy, ref=cs.ref, pin=pin))
            comps.append(Component(ref=cs.ref, value=cs.value, pads=pads,
                                   footprint=cs.footprint, part_class=cs.part_class))
        for name, meta in self.net_meta.items():
            net = nets.get(name)
            if net is None:
                continue
            net.net_class = meta.net_class
            for (vx, vy) in meta.vias:
                net.vias.append(Via(x_mm=vx + ox, y_mm=vy + oy))
            for (x0, y0, x1, y1, w) in meta.segments:
                net.features.append(NetFeature(
                    layer="F.Cu", width_mm=w,
                    length_mm=((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5,
                    segments=[((x0 + ox, y0 + oy), (x1 + ox, y1 + oy))]))
            if meta.poured:
                # A rectangular pour plus a couple of stitching vias, mirroring a
                # real ground plane: thin inter-via runs must NOT read as necking.
                net.fill_regions.append(
                    (None, [(ox, oy), (60.0 + ox, oy), (60.0 + ox, 40.0 + oy), (ox, 40.0 + oy)]))
                net.vias.extend([Via(x_mm=5.0 + ox, y_mm=35.0 + oy),
                                 Via(x_mm=55.0 + ox, y_mm=35.0 + oy)])
        dd.nets = nets
        dd.components = comps
        dd.pin_types = dict(self.pin_types)
        dd.nc_pins = set(self.nc_pins)
        return dd


# --------------------------------------------------------------------------
# Board 1: MCU core -- reproduces the flagship #85 false-positive patterns.
# --------------------------------------------------------------------------
#
#  * GND assigned net_class "Power" (KiCad bundles GND+VCC in one width class).
#    A correct classify_net keeps GND a *ground* rail, so U1 reaches ground and
#    the decoupling caps are seen bridging to ground.  [the flagship FP]
#  * GND poured with stitching vias -> trace_necking / orphan_via must skip it.
#  * U2, an 8-pin fuel gauge whose only rail is VBAT (a power net, no GND-named
#    net at all) -> unpowered_ic must NOT demand a ground-named rail.
#  * Full decoupling / crystal caps / I2C + reset pull-ups / SWD test points, so
#    the "is the support part present?" checks all resolve to pass.
def _mcu_core() -> BoardSpec:
    comps = [
        # 12-pin MCU. Two rows of six; every pad on its own net access point.
        CompSpec("U1", "STM32", [
            ("1", 10.0, 10.0, "VCC"),   ("2", 11.0, 10.0, "GND"),
            ("3", 12.0, 10.0, "SDA"),   ("4", 13.0, 10.0, "SCL"),
            ("5", 14.0, 10.0, "NRST"),  ("6", 15.0, 10.0, "VDDA"),
            ("7", 10.0, 14.0, "SWDIO"), ("8", 11.0, 14.0, "SWCLK"),
            ("9", 12.0, 14.0, "XIN"),   ("10", 13.0, 14.0, "XOUT"),
            ("11", 14.0, 14.0, "PB0"),  ("12", 15.0, 14.0, "PB1"),
        ], footprint="Package_QFP:LQFP-12"),
        # 8-pin fuel gauge: only rail is VBAT (power). No ground-named net.
        CompSpec("U2", "MAX17048", [
            ("1", 30.0, 10.0, "VBAT"),   ("2", 31.0, 10.0, "BAT_NEG"),
            ("3", 32.0, 10.0, "SDA"),    ("4", 33.0, 10.0, "SCL"),
            ("5", 30.0, 12.0, "ALERT"),  ("6", 31.0, 12.0, "QSTRT"),
            ("7", 32.0, 12.0, "VBAT"),   ("8", 33.0, 12.0, "GPOUT"),
        ], footprint="Package_TO_SOT_SMD:TSOT-23-8"),
        # Decoupling: 0.1uF per rail + a 10uF bulk on VCC.
        CompSpec("C1", "0.1uF", [("1", 10.0, 8.0, "VCC"), ("2", 11.0, 8.0, "GND")]),
        CompSpec("C2", "0.1uF", [("1", 15.0, 8.0, "VDDA"), ("2", 16.0, 8.0, "GND")]),
        CompSpec("C5", "10uF", [("1", 8.0, 8.0, "VCC"), ("2", 8.0, 9.0, "GND")]),
        CompSpec("C6", "0.1uF", [("1", 30.0, 8.0, "VBAT"), ("2", 31.0, 8.0, "GND")]),
        # Crystal + its two load caps to ground.
        CompSpec("Y1", "8MHz", [("1", 12.0, 18.0, "XIN"), ("2", 13.0, 18.0, "XOUT")]),
        CompSpec("C3", "18pF", [("1", 12.0, 20.0, "XIN"), ("2", 12.5, 21.0, "GND")]),
        CompSpec("C4", "18pF", [("1", 13.0, 20.0, "XOUT"), ("2", 13.5, 21.0, "GND")]),
        # I2C pull-ups to VCC, reset pull-up to VCC.
        CompSpec("R1", "4k7", [("1", 12.0, 6.0, "SDA"), ("2", 12.0, 5.0, "VCC")]),
        CompSpec("R2", "4k7", [("1", 13.0, 6.0, "SCL"), ("2", 13.0, 5.0, "VCC")]),
        CompSpec("R3", "10k", [("1", 14.0, 6.0, "NRST"), ("2", 14.0, 5.0, "VCC")]),
        # PB0 -> LED via series resistor (a complete indicator).
        CompSpec("R4", "330", [("1", 20.0, 14.0, "PB0"), ("2", 21.0, 14.0, "LEDA")]),
        CompSpec("LED1", "GRN", [("1", 22.0, 14.0, "LEDA"), ("2", 23.0, 14.0, "GND")]),
        # SWD test points.
        CompSpec("TP1", None, [("1", 10.0, 16.0, "SWDIO")]),
        CompSpec("TP2", None, [("1", 11.0, 16.0, "SWCLK")]),
        # Header exposing the MCU/gauge signals (so none is a single-pin stub).
        CompSpec("J1", "HDR", [
            ("1", 40.0, 14.0, "PB1"),   ("2", 40.0, 15.0, "GND"),
            ("3", 40.0, 16.0, "ALERT"), ("4", 40.0, 17.0, "GPOUT"),
            ("5", 40.0, 18.0, "QSTRT"),
        ]),
        # Battery connector: gives VBAT and BAT_NEG a second pad each.
        CompSpec("J2", "BATT", [("1", 34.0, 10.0, "VBAT"), ("2", 34.0, 11.0, "BAT_NEG")]),
    ]
    net_meta = {
        # VCC carries a uniform-width run -> a clean trace_necking candidate.
        "VCC": NetMeta(net_class="Power",
                       segments=[(8.0, 8.0, 10.0, 10.0, 0.5), (10.0, 10.0, 12.0, 5.0, 0.5)]),
        "VDDA": NetMeta(net_class="Power"),
        "VBAT": NetMeta(net_class="Power"),
        # The flagship trap: GND in the "Power" width class, and poured. Its wide
        # plane feed plus a thin stitch run would read as a neck-down if the
        # poured-net skip regressed -- with the skip it is correctly ignored.
        "GND": NetMeta(net_class="Power", poured=True,
                       segments=[(0.0, 35.0, 30.0, 35.0, 0.5), (30.0, 35.0, 33.0, 35.0, 0.15)]),
    }
    pin_types = {
        ("U1", "1"): "power_in", ("U1", "2"): "power_in", ("U1", "6"): "power_in",
        ("U2", "1"): "power_in", ("U2", "7"): "power_in",
    }
    must_pass = {
        "unpowered_ic":
            "U1 reaches GND (net_class 'Power' must still classify as ground); "
            "U2's only rail is VBAT (power) -- neither is a missing-rail case.",
        "decoupling_adequacy":
            "each supply rail has a 0.1uF cap bridging to GND, which is only "
            "seen if GND classifies as ground despite its 'Power' net class.",
        "trace_necking":
            "GND is poured; its thin stitching runs must not read as a neck-down.",
        "orphan_or_redundant_via":
            "GND stitching vias sit on a pour, not on a trace endpoint -- not orphans.",
        "i2c_pullup_presence": "SDA/SCL both have a 4k7 pull-up to VCC.",
        "reset_pullup_presence": "NRST has a 10k pull-up to VCC.",
        "crystal_load_caps": "Y1 has two 18pF load caps to GND.",
        "led_series_resistor": "LED1 is fed through R4, not straight across a rail.",
        "debug_port_test_access": "SWDIO/SWCLK each land on a test point.",
        "floating_or_single_pin_net": "every net has >= 2 pins.",
    }
    return BoardSpec(
        name="mcu_core",
        description="Realistic STM32 core reproducing the #85 real-board FP traps.",
        comps=comps, net_meta=net_meta, pin_types=pin_types, must_pass=must_pass)


TRUST_BOARDS: List[BoardSpec] = [_mcu_core()]
