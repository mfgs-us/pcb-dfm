"""Core / prepreg lamination validity.

The real lamination rules turn on which dielectrics are *cores* (cured laminate,
copper already bonded, rigid) and which are *prepreg* (uncured glass and resin
that becomes the adhesive under heat and pressure). Every adapter used to collapse
that distinction to a single "dielectric" kind, so these rules were not expressible
at all; the material is now carried through, and this is what it buys:

  * a build with no core has nothing rigid to register to,
  * cores do not bond to each other -- prepreg is the adhesive, so two adjacent
    cores is not a construction, and
  * too little prepreg between cores starves the bond.

Material is absent from most real input and will be for a while, so every rule
degrades on its own: no material data at all is not_applicable, and a rule whose
specific layers are unknown is skipped rather than assumed.

Mid-plane material asymmetry (a core mirroring a prepreg) is deliberately *not*
reported here -- it belongs to ``stackup_symmetry``, which now compares material
alongside kind. Reporting it in both places would double-count one defect.
"""

from __future__ import annotations

from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ._stackup_struct import fault_result, na, ordered_stack

# Minimum pressed prepreg between two cores for a reliable bond (mm). Roughly two
# 1080 sheets; a single thin sheet is a re-quote, not a hard reject.
_MIN_PREPREG_BOND_MM = 0.1


@register_check("stackup_lamination_validity")
def run_stackup_lamination_validity(ctx: CheckContext):
    layers, reason = ordered_stack(ctx)
    if reason is not None:
        return na(ctx, reason)

    dielectrics = [ly for ly in layers if ly.kind == "dielectric"]
    if not any(getattr(ly, "material", None) in ("core", "prepreg") for ly in dielectrics):
        return na(
            ctx,
            "Stackup carries no core/prepreg material data, so the lamination rules "
            "are not evaluable. Supply it via IPC-2581 layer functions (CORE/PREPREG), "
            "a KiCad stackup layer type, ODB++ DIELECTRIC_TYPE, or a sidecar layer "
            "'material' field.",
        )

    p = (ctx.check_def.raw or {}).get("params", {}) or {}
    min_bond_mm = float(p.get("min_prepreg_bond_mm", _MIN_PREPREG_BOND_MM))

    faults: List[str] = []
    notes: List[str] = []

    cores = [ly for ly in dielectrics if getattr(ly, "material", None) == "core"]
    unknown = [ly for ly in dielectrics if getattr(ly, "material", None) not in ("core", "prepreg")]

    # 1. At least one core. Only assert it when every dielectric named its
    #    material -- an unknown dielectric could be the core.
    if not cores:
        if unknown:
            notes.append(
                f"No core declared, but {len(unknown)} dielectric(s) do not state a "
                f"material, so one of them may be the core. Rule skipped rather than "
                f"assumed."
            )
        else:
            faults.append(
                "Stackup declares no core: every dielectric is prepreg. A build needs "
                "at least one cured core to register and press against."
            )

    # 2/3. Walk consecutive core pairs and look at what lies between them.
    #      Copper is not a bonding agent -- two cores separated only by a foil
    #      still have nothing holding them together -- so the pair, not a run of
    #      touching dielectrics, is the unit. Grouping by contiguous dielectric
    #      run instead made both rules unreachable on any well-formed stack:
    #      every run there is length 1, so no run ever held two cores.
    for core_a, core_b, between in _core_pairs(layers):
        mats = [getattr(ly, "material", None) for ly in between]
        names = f"'{getattr(core_a, 'name', '?')}' and '{getattr(core_b, 'name', '?')}'"

        if "prepreg" not in mats:
            if any(m not in ("core", "prepreg") for m in mats):
                # An unnamed dielectric between them could be the prepreg. Same
                # degradation rule as the no-core case: skip, do not assume.
                notes.append(
                    f"{names} have no declared prepreg between them, but an "
                    f"intervening dielectric states no material and may be it. "
                    f"Core-to-core rule skipped rather than assumed."
                )
            else:
                faults.append(
                    f"Two cores laminate directly against each other ({names}) with "
                    f"no prepreg between them. Cores are already cured and do not "
                    f"bond to one another; prepreg is the adhesive."
                )
            continue

        # Bond thickness, only when every prepreg between them states one.
        preg = [ly for ly in between if getattr(ly, "material", None) == "prepreg"]
        thicknesses = [ly.thickness_mm for ly in preg if ly.thickness_mm is not None]
        if len(thicknesses) == len(preg) and thicknesses:
            total = sum(thicknesses)
            if total < min_bond_mm:
                faults.append(
                    f"Only {total * 1000.0:.0f} um of prepreg bonds the cores "
                    f"{names} (recommended >= {min_bond_mm * 1000.0:.0f} um). A "
                    f"thin single sheet risks a resin-starved bond."
                )

    if unknown and cores:
        notes.append(
            f"{len(unknown)} dielectric(s) state no material; rules needing them were "
            f"skipped."
        )

    return fault_result(
        ctx, faults, notes,
        clean_message=(
            f"Lamination construction is valid: {len(cores)} core(s), "
            f"{sum(1 for ly in dielectrics if getattr(ly, 'material', None) == 'prepreg')} "
            f"prepreg layer(s), no core-to-core lamination."
        ),
    )


def _core_pairs(layers) -> List[tuple]:
    """``(core, next_core, dielectrics_between)`` for each consecutive core pair.

    Copper is skipped rather than treated as a separator: a foil between two cores
    is not what bonds them, so ``[core, In1.Cu, core]`` is as much a core-to-core
    lamination as ``[core, core]`` is. Only dielectrics are reported as "between",
    since they are what the bonding rules weigh.
    """
    dielectrics = [ly for ly in layers if ly.kind == "dielectric"]
    core_idx = [i for i, ly in enumerate(dielectrics)
                if getattr(ly, "material", None) == "core"]
    return [
        (dielectrics[a], dielectrics[b], dielectrics[a + 1:b])
        for a, b in zip(core_idx, core_idx[1:])
    ]
