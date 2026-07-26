"""Design advisory: power/ground nets should not be routed thinner than signals.

A power or ground rail carries more current than a signal, so its traces should
be at least as wide. A rail routed with signal-width (or thinner) trace is a
current + IR-drop bottleneck -- a classic "forgot to widen the power net" miss.

This is a *relative* test, so it needs no absolute width rule: a power/ground
net whose typical (median) routed width is narrower than the board's own median
signal width is flagged. Median (not minimum) is used so a single pad-entry neck
does not trip it -- only a net routed systematically thin does.

Needs routed trace widths (e.g. a KiCad board) and a signal baseline to compare
against; otherwise not_applicable.
"""

from __future__ import annotations

import math
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_net
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na

_MIN_SIGNAL_SEGMENTS = 5   # need a real baseline before judging
_MIN_SEG_LEN_MM = 0.5      # ignore sub-0.5 mm stubs (pad breakouts)


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _net_seg_widths(net) -> List[float]:
    out: List[float] = []
    for (seg, _layer, width) in net.route_segments():
        if width is None or width <= 0:
            continue
        (x0, y0), (x1, y1) = seg
        if math.hypot(x1 - x0, y1 - y0) < _MIN_SEG_LEN_MM:
            continue
        out.append(float(width))
    return out


@register_check("power_ground_trace_width")
def run_power_ground_trace_width(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    nets = list(getattr(dd, "nets", {}).values()) if dd is not None else []
    if not nets:
        return na(ctx, "Needs a netlist with routed trace widths.")

    signal_widths: List[float] = []
    power_nets: List[Tuple[str, float]] = []   # (name, median width)
    for net in nets:
        widths = _net_seg_widths(net)
        if not widths:
            continue
        fn = classify_net(net.name, net.net_class)
        if fn in ("power", "ground"):
            power_nets.append((net.name, _median(widths)))
        else:
            signal_widths.extend(widths)

    if len(signal_widths) < _MIN_SIGNAL_SEGMENTS:
        return na(ctx, "Too few routed signal traces to form a width baseline.")
    if not power_nets:
        return na(ctx, "No routed power/ground nets to evaluate.")

    baseline = _median(signal_widths)
    thin = [(name, w) for (name, w) in power_nets if w < baseline - 1e-6]

    count = len(thin)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        f"All {len(power_nets)} power/ground net(s) are routed at "
                        f"least as wide as the median signal trace ({baseline:.3f} mm).")
    thin.sort(key=lambda nw: nw[1])
    name, w = thin[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} power/ground net(s) are routed narrower than the median signal "
        f"trace ({baseline:.3f} mm) -- e.g. '{name}' at {w:.3f} mm. Widen the rail to "
        f"cut IR drop and current crowding.",
        ViolationLocation(layer="Copper", x_mm=0.0, y_mm=0.0,
                          notes=f"Power/ground net '{name}' median width {w:.3f} mm."),
    )
