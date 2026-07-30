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

    # 2. Core-to-core adjacency, walking the dielectric runs between coppers. Two
    #    cores with only copper between them still needs prepreg somewhere in that
    #    run, so the run -- not the raw layer list -- is the right unit.
    for run in _dielectric_runs(layers):
        mats = [getattr(ly, "material", None) for ly in run]
        if mats.count("core") >= 2 and "prepreg" not in mats:
            names = ", ".join(f"'{getattr(ly, 'name', '?')}'" for ly in run)
            faults.append(
                f"Two cores laminate directly against each other ({names}) with no "
                f"prepreg between them. Cores are already cured and do not bond to "
                f"one another; prepreg is the adhesive."
            )

        # 3. Prepreg bond thickness between cores, when the run has both and every
        #    prepreg in it states a thickness.
        if mats.count("core") >= 2 and "prepreg" in mats:
            preg = [ly for ly in run if getattr(ly, "material", None) == "prepreg"]
            thicknesses = [ly.thickness_mm for ly in preg if ly.thickness_mm is not None]
            if len(thicknesses) == len(preg) and thicknesses:
                total = sum(thicknesses)
                if total < min_bond_mm:
                    faults.append(
                        f"Only {total * 1000.0:.0f} um of prepreg bonds the cores in "
                        f"this run (recommended >= {min_bond_mm * 1000.0:.0f} um). A "
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


def _dielectric_runs(layers) -> List[List]:
    """Consecutive dielectric layers, grouped into the runs between copper layers.

    A run is what actually gets pressed together, so it is the unit the bonding
    rules apply to. A single dielectric between two coppers is a run of one and
    can never violate a core-to-core rule.
    """
    runs: List[List] = []
    current: List = []
    for ly in layers:
        if ly.kind == "dielectric":
            current.append(ly)
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs
