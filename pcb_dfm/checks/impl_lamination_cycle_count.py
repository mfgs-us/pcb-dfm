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


def _reach(spans: List[tuple], start: int, forward: bool) -> int:
    """How far microvia levels chain inward from a surface copper layer.

    Each level is drilled and plated on the foil the level before it added, so
    they only count when they connect back to the surface: L1->L2 then L2->L3 is
    a 2-deep build-up, while a lone L2->L3 with nothing above it is not build-up
    at all. Iterating to a fixed point handles stacked and staggered spans, and
    spans arriving in any order.
    """
    frontier = start
    changed = True
    while changed:
        changed = False
        for lo, hi in spans:
            if forward and lo <= frontier < hi:
                frontier, changed = hi, True
            elif not forward and lo < frontier <= hi:
                frontier, changed = lo, True
    return frontier


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

    micro_spans: List[tuple] = []
    buried_spans: List[tuple] = []
    blind_spans: List[tuple] = []
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
            micro_spans.append((lo, hi))
        elif v.via_type == "buried":
            buried_spans.append((lo, hi))
        elif v.via_type == "blind":
            # Drilled to depth after its outer foil is on, so it needs no press of
            # its own. Recorded for the note, deliberately not counted as build-up:
            # counting it turned one F.Cu->In2 blind via into "2+N+0, 3 cycles".
            blind_spans.append((lo, hi))

    # Build-up depth is how far the microvia levels reach in from each surface.
    # It has to be walked, not read off a single span: a stacked 2+N+2 build is
    # L1->L2 *plus* L2->L3, and testing only spans that touch a surface dropped
    # the second level and called the board 1+N+1.
    top_buildup = _reach(micro_spans, start=1, forward=True) - 1
    bottom_buildup = total_copper - _reach(micro_spans, start=total_copper,
                                           forward=False)

    # Cycles: one press for the core, one more for each build-up level added per
    # side (they go on one at a time), and one for a buried-via sub-core that has
    # to be pressed, drilled and plated before anything laminates onto it. The
    # sub-lamination is an extra press whether or not there is also build-up.
    buildup = max(top_buildup, bottom_buildup)
    cycles = 1 + buildup + (1 if buried_spans else 0)

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
            f"are added -- one press cycle on top of the build-up."
        )
    if blind_spans:
        notes.append(
            f"{len(blind_spans)} blind via span(s) are drilled to depth after "
            f"lamination, so they add no press cycle of their own."
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
