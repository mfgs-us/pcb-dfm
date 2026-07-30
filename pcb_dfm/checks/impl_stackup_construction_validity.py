"""Stackup construction validity -- is this layer stack a physically possible build?

Every other stackup check assumes the stack is well-formed and none of them
verifies it. ``stackup_symmetry`` pairs layer *k*-from-top with *k*-from-bottom;
``microvia_geometry``'s span-depth helper slices the layers between two coppers.
Both assume strict copper/dielectric alternation in top-to-bottom order. Violate
that and they return a confident number computed from nonsense, with nothing in
the report to say so. This check is the precondition the rest of the domain
silently relies on.

There is also a live path that produces a malformed stack from valid input: the
IPC-2581 adapter falls back to "an Er implies dielectric, otherwise copper" when
a ``<StackupLayer>`` declares no layer function. On a file that declares none at
all, that yields an all-copper stack -- which the numeric checks would happily
consume.

What is *not* a fault matters as much as what is. Adjacent dielectrics are normal
construction (multiple prepreg sheets between cores), so they are explicitly
allowed; an odd copper count is legal but unusual, so it is reported as an
observation that never changes the status.
"""

from __future__ import annotations

from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ._stackup_struct import fault_result, na, ordered_stack

# Physically plausible bounds for a laminate dielectric constant. Outside these a
# value is a data error (a units mix-up or a placeholder), not a exotic material.
_ER_MIN = 2.0
_ER_MAX = 10.0


def _params(ctx: CheckContext) -> dict:
    return (ctx.check_def.raw or {}).get("params", {}) or {}


@register_check("stackup_construction_validity")
def run_stackup_construction_validity(ctx: CheckContext):
    layers, reason = ordered_stack(ctx)
    if reason is not None:
        return na(ctx, reason)

    p = _params(ctx)
    er_min = float(p.get("er_min", _ER_MIN))
    er_max = float(p.get("er_max", _ER_MAX))

    faults: List[str] = []
    notes: List[str] = []
    n = len(layers)

    def label(i: int) -> str:
        name = getattr(layers[i], "name", None)
        return f"layer {i + 1}" + (f" ('{name}')" if name else "")

    # 1. Copper cannot laminate to copper. Adjacent dielectrics, by contrast, are
    #    ordinary construction (two prepreg sheets between cores) and must stay
    #    silent -- that is the false positive this check exists alongside.
    for i in range(n - 1):
        if layers[i].kind == "copper" and layers[i + 1].kind == "copper":
            faults.append(
                f"{label(i)} and {label(i + 1)} are both copper with no dielectric "
                f"between them. That is not a build: either the stackup is missing a "
                f"dielectric or the source was parsed wrong. Every other stackup "
                f"check assumes alternation, so their numbers are unreliable until "
                f"this is fixed."
            )

    # 2. A board's outer surfaces are copper. A stack that starts or ends on a
    #    dielectric is missing its outer foil.
    for i, where in ((0, "starts"), (n - 1, "ends")):
        if layers[i].kind != "copper":
            faults.append(
                f"Stackup {where} on a {layers[i].kind} layer ({label(i)}); the outer "
                f"surfaces of a board are copper. The outer foil is missing from the "
                f"declared stack."
            )

    # 3. Duplicate names mean two records merged or one was emitted twice; layer
    #    identity is how spans and artwork are matched, so it has to be unique.
    seen: dict = {}
    for i in range(n):
        name = getattr(layers[i], "name", None)
        if not name:
            continue
        key = str(name).strip().lower()
        if key in seen:
            faults.append(
                f"Duplicate layer name '{name}' at {label(seen[key])} and {label(i)}. "
                f"Layer names identify via spans and artwork, so they must be unique."
            )
        else:
            seen[key] = i

    # 4. Data sanity. These are faults in the *input*, reported as such rather
    #    than dressed up as manufacturability findings.
    for i in range(n):
        ly = layers[i]
        t = getattr(ly, "thickness_mm", None)
        if t is not None and t <= 0:
            faults.append(
                f"{label(i)} has a non-positive thickness ({t} mm). A layer with no "
                f"thickness cannot be built or measured."
            )
        er = getattr(ly, "er", None)
        if er is None:
            continue
        if er <= 1.0:
            faults.append(
                f"{label(i)} declares Er = {er:g}, which is physically impossible "
                f"(vacuum is 1.0). The value is wrong, not exotic."
            )
        elif not (er_min <= er <= er_max):
            faults.append(
                f"{label(i)} declares Er = {er:g}, outside the plausible laminate "
                f"range {er_min:g}-{er_max:g}. Check the units and the source field."
            )

    # 5. Observations that never change the status.
    copper_count = sum(1 for ly in layers if ly.kind == "copper")
    if copper_count % 2 == 1:
        notes.append(
            f"Stackup has an odd copper-layer count ({copper_count}). That is legal "
            f"but unusual: it cannot be symmetric about the mid-plane, and most fabs "
            f"re-quote it. Not a defect."
        )

    return fault_result(
        ctx, faults, notes,
        clean_message=(
            f"Stackup is a well-formed {copper_count}-copper-layer build: copper and "
            f"dielectric alternate, both outer surfaces are copper, and layer names "
            f"are unique."
        ),
    )
