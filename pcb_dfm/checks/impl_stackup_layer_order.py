"""Layer ordering vs layer naming -- is the stack ordered the way it claims?

A stack whose physical order disagrees with its own layer names is a mirrored or
transposed build, and every *geometric* check in the catalogue passes it cleanly:
the trace widths, clearances and rings are all still correct. The board comes back
electrically wrong, not geometrically wrong, which is exactly why nothing we
already run can see it.

Order trust also varies by source. ODB++ states an explicit matrix ``ROW`` and the
adapter sorts by it; IPC-2581 carries a stackup sequence attribute (now honoured
by that adapter); a KiCad board and a sidecar list are trusted in file order. This
check is the cross-examination of whatever order we ended up with, using the one
independent witness available -- what the layers call themselves.

Conservative by construction: layer naming conventions vary wildly between tools,
so an unrecognised naming scheme yields not_applicable rather than a guess.
Reporting a correctly built board as transposed would be a far worse failure than
staying quiet.
"""

from __future__ import annotations

from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ._stackup_struct import fault_result, inner_index, layer_side, na, ordered_stack


@register_check("stackup_layer_order")
def run_stackup_layer_order(ctx: CheckContext):
    layers, reason = ordered_stack(ctx)
    if reason is not None:
        return na(ctx, reason)

    copper = [ly for ly in layers if ly.kind == "copper"]
    if len(copper) < 2:
        return na(ctx, "Fewer than two copper layers; there is no order to validate.")

    names = [getattr(ly, "name", None) for ly in copper]
    indices = [inner_index(nm) for nm in names]
    sides = [layer_side(nm) for nm in names]

    faults: List[str] = []
    notes: List[str] = []

    # The outer copper layers should call themselves top and bottom. A named
    # *inner* layer in an outer position is the clearest transposition signal
    # there is, so it is a fault; a name that says nothing is just silence.
    first_side, last_side = sides[0], sides[-1]
    if first_side == "bottom" and last_side == "top":
        faults.append(
            f"Stack runs bottom-to-top: the first layer is '{names[0]}' and the last "
            f"is '{names[-1]}'. The stackup is reversed relative to its own names, so "
            f"every layer-dependent result (spans, adjacency, registration) is "
            f"mirrored."
        )
    else:
        if first_side == "bottom":
            faults.append(
                f"First copper layer is '{names[0]}', which names the bottom side. "
                f"The stack is ordered top-to-bottom, so position 1 must be the top "
                f"layer."
            )
        if last_side == "top":
            faults.append(
                f"Last copper layer is '{names[-1]}', which names the top side. The "
                f"stack is ordered top-to-bottom, so the final position must be the "
                f"bottom layer."
            )

    # Inner-layer ordinals must ascend top-to-bottom. Only assert this when at
    # least two inner names actually parse -- one index proves nothing about
    # order, and zero means the convention is unrecognised.
    inner_pairs = [(pos, idx) for pos, idx in enumerate(indices)
                   if idx is not None and 0 < pos < len(copper) - 1]
    if len(inner_pairs) >= 2:
        prev_pos, prev_idx = inner_pairs[0]
        for pos, idx in inner_pairs[1:]:
            if idx < prev_idx:
                faults.append(
                    f"Inner-layer numbering decreases down the stack: '{names[prev_pos]}' "
                    f"(position {prev_pos + 1}) is followed by '{names[pos]}' (position "
                    f"{pos + 1}). Inner layers are numbered from the top, so the stack "
                    f"order and the layer names disagree."
                )
            elif idx == prev_idx:
                faults.append(
                    f"Two inner copper layers claim the same ordinal: '{names[prev_pos]}' "
                    f"and '{names[pos]}'."
                )
            prev_pos, prev_idx = pos, idx

        # Gaps: In1, In2, In4 means a layer is absent from the declared stack.
        # Distinct from stackup_artwork_consistency, which counts artwork films;
        # this reads what the names themselves imply.
        ordinals = sorted(idx for _pos, idx in inner_pairs)
        if len(ordinals) >= 2 and ordinals == sorted(set(ordinals)):
            expected = list(range(ordinals[0], ordinals[-1] + 1))
            missing = [o for o in expected if o not in ordinals]
            if missing:
                faults.append(
                    f"Inner-layer numbering has gaps: found {ordinals}, missing "
                    f"{missing}. A layer named in the sequence is absent from the "
                    f"stackup."
                )
    elif not any(idx is not None for idx in indices) and not any(sides):
        return na(
            ctx,
            "Copper layer names follow no recognised convention (expected F.Cu/In1.Cu/"
            "B.Cu, L1..Ln, or top/inner-n/bottom), so physical order cannot be checked "
            "against naming.",
        )
    elif len(copper) > 2 and len(inner_pairs) < 2:
        notes.append(
            f"Only {len(inner_pairs)} inner copper layer name(s) parsed to an ordinal, "
            f"so inner-layer ordering was not validated; outer-side naming was."
        )

    return fault_result(
        ctx, faults, notes,
        clean_message=(
            f"Layer order agrees with layer naming across {len(copper)} copper layers "
            f"('{names[0]}' -> '{names[-1]}')."
        ),
    )
