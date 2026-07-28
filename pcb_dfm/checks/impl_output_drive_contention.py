"""Output drive contention -- two push-pull outputs fighting on one net.

Needs schematic pin electrical types (``DesignData.pin_types``); there is no way
to tell a driver from a receiver without them, so this is not_applicable on a
bare-Gerber board (no fallback exists -- direction can't be inferred from copper).
"""

from __future__ import annotations

from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design

# Pins that are *meant* to share a net (multi-driver buses) -- their presence
# means the outputs aren't in contention.
_SHARED_BUS = {"tri_state", "open_collector", "open_emitter"}


@register_check("output_drive_contention")
def run_output_drive_contention(ctx: CheckContext) -> CheckResult:
    rd = resolve_design(ctx.design_data)
    if rd is None:
        return na(ctx, "No netlist + BOM to resolve nets; not applicable.")
    if not rd.has_pin_types():
        return na(ctx, "No schematic pin types; drive-contention review not applicable.")

    bad: List[str] = []
    for net, types in rd.net_pin_types.items():
        if net.startswith("unconnected-"):
            continue  # KiCad placeholder for an unconnected pad, not a real net
        if types & _SHARED_BUS:
            continue  # a shared bus (tri-state / open-drain) is not contention
        # Count push-pull output pins on the net.
        outs = sum(1 for (ref, pin), n in rd.idx.pad_net.items()
                   if n == net and rd.dd.pin_types.get((ref, pin)) == "output")
        if outs >= 2:
            bad.append(net)
    bad.sort()
    flagged = bool(bad)
    msg = (f"{len(bad)} net(s) with two or more push-pull outputs driving each "
           f"other: {', '.join(bad[:8])} -> contention/​short.") if flagged \
        else "No output drive contention."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
