"""Implied lamination cycles and HDI build class.

The number of press cycles a build needs is what a quote is actually priced on,
and it is derivable rather than guessed: microvia spans on the outer dielectrics
give the ``N+N`` prefix and suffix, and buried spans imply a sub-lamination that
must be pressed before the outer layers go on. A designer who sees "this is a
2+N+2 build needing 3 press cycles" learns something no other check reports.

**Informational only, by design.** The derivation is objective; "too many cycles"
is a cost judgment, not a manufacturability rule, and this project does not encode
cost opinions as findings. So this check never returns worse than a pass with an
informational note. If it ever grows a threshold it needs a sourced fab capability
limit, not an intuition about what is expensive.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation
from ._stackup_struct import na, ordered_stack


def _copper_positions(layers) -> Dict[str, int]:
    """Copper layer name (lowercased) -> its ordinal among copper layers, 1-based."""
    out: Dict[str, int] = {}
    n = 0
    for ly in layers:
        if ly.kind != "copper":
            continue
        n += 1
        name = getattr(ly, "name", None)
        if name:
            out[str(name).strip().lower()] = n
    return out


def _ordinal(positions: Dict[str, int], name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    return positions.get(str(name).strip().lower())


@register_check("lamination_cycle_count")
def run_lamination_cycle_count(ctx: CheckContext) -> CheckResult:
    layers, reason = ordered_stack(ctx)
    if reason is not None:
        return na(ctx, reason)

    dd = ctx.design_data
    vias = [v for net in (dd.nets or {}).values() for v in net.vias] if dd is not None else []
    if not vias:
        return na(
            ctx,
            "No via topology in the design data, so the lamination sequence cannot be "
            "derived. Supply vias with layer spans (IPC-2581, ODB++ or a KiCad board).",
        )

    positions = _copper_positions(layers)
    total_copper = sum(1 for ly in layers if ly.kind == "copper")

    # Microvia build-up depth: how many copper layers in from each surface the
    # microvia spans reach. That count is the N+N prefix/suffix.
    top_buildup = 0
    bottom_buildup = 0
    buried_spans: List[tuple] = []
    unresolved = 0

    for v in vias:
        a = _ordinal(positions, v.from_layer)
        b = _ordinal(positions, v.to_layer)
        if a is None or b is None:
            if v.via_type in ("micro", "blind", "buried"):
                unresolved += 1
            continue
        lo, hi = min(a, b), max(a, b)
        if v.via_type == "micro":
            if lo == 1:
                top_buildup = max(top_buildup, hi - 1)
            elif hi == total_copper:
                bottom_buildup = max(bottom_buildup, total_copper - lo)
        elif v.via_type == "buried":
            buried_spans.append((lo, hi))
        elif v.via_type == "blind":
            # A blind via is drilled after its outer foil is on, so it does not
            # itself add a press cycle -- but it does mean the outer layers are a
            # separate build-up step from the core.
            if lo == 1:
                top_buildup = max(top_buildup, hi - 1)
            elif hi == total_copper:
                bottom_buildup = max(bottom_buildup, total_copper - lo)

    # Cycles: one press for the core, one more for each build-up layer added per
    # side (they go on one at a time), and one for a buried-via sub-core that has
    # to be pressed before anything is laminated onto it.
    buildup = max(top_buildup, bottom_buildup)
    cycles = 1 + buildup
    if buried_spans and buildup == 0:
        # A buried span with no build-up is still a sub-lamination: press the
        # inner core set, drill and plate it, then press the outer foils on.
        cycles = 2

    inner = max(0, total_copper - top_buildup - bottom_buildup)
    build_class = (
        f"{top_buildup}+N+{bottom_buildup}" if (top_buildup or bottom_buildup)
        else "through-hole (single lamination)"
    )

    notes: List[str] = []
    if top_buildup or bottom_buildup:
        notes.append(
            f"Build class {build_class}: {top_buildup} build-up layer(s) on top and "
            f"{bottom_buildup} on the bottom over an N={inner}-layer core, implying "
            f"{cycles} lamination cycle(s). Each cycle is a separate press, drill and "
            f"plate pass -- the dominant cost and lead-time driver in an HDI quote."
        )
    else:
        notes.append(
            f"Through-hole build over {total_copper} copper layers: a single lamination "
            f"cycle, no sequential build-up."
        )
    if buried_spans:
        notes.append(
            f"{len(buried_spans)} buried via span(s) require the inner layers to be "
            f"pressed, drilled and plated as a sub-lamination before the outer layers "
            f"are added."
        )
    if unresolved:
        notes.append(
            f"{unresolved} blind/buried/micro via(s) name layers that are not in the "
            f"stackup, so they were excluded from the derivation; the cycle count is a "
            f"floor."
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="pass",
        severity="info",
        score=100.0,
        metric=MetricResult(kind="count", units="count",
                            measured_value=float(cycles)),
        violations=[Violation(severity="info", message=m, location=None) for m in notes],
    ).finalize()
