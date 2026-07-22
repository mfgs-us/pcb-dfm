"""
Per-check remediation guidance: how to fix a finding and what it costs to ship
it as-is. Keyed by check id and surfaced in the reports so a finding reads as an
*action*, not just a measurement.

Kept as data (not prose in the formatters) so guidance is testable and every
registered check is covered — see ``tests/test_remediation.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Remediation:
    fix: str      # concrete action to resolve the finding
    impact: str   # yield/cost consequence of shipping it unaddressed


GUIDANCE: Dict[str, Remediation] = {
    # --- copper geometry ---------------------------------------------------
    "min_trace_width": Remediation(
        "Widen the trace, or move the net to a fab/copper class that supports the width.",
        "Etched-open (broken) traces → scrapped boards."),
    "min_trace_spacing": Remediation(
        "Increase copper-to-copper clearance or thin the copper on that layer.",
        "Shorts/bridging between conductors → scrap."),
    "copper_to_edge_distance": Remediation(
        "Pull copper back from the board outline (typically ≥ 0.25 mm).",
        "Exposed/​shorted copper at the routed edge."),
    "copper_sliver_width": Remediation(
        "Reshape pours/clearances so no thin copper sliver forms.",
        "Slivers lift during processing → shorts and debris."),
    "acid_trap_angle": Remediation(
        "Open acute copper angles to ≥ 90° (teardrops or reshaped corners).",
        "Trapped etchant over-etches the corner → opens."),
    "copper_density_balance": Remediation(
        "Balance copper across the board (add thieving/hatch in sparse areas).",
        "Warp and uneven etch/plating."),
    "etch_compensation_margin": Remediation(
        "Raise the smallest feature above the fab's etch floor, or add etch compensation.",
        "Yield loss on features sitting at the process limit."),

    # --- drill / via -------------------------------------------------------
    "min_annular_ring": Remediation(
        "Enlarge the pad or shrink the drill so the ring meets the minimum.",
        "Drill breakout / broken plated barrels."),
    "min_drill_size": Remediation(
        "Enlarge the drill to the fab's minimum, or use laser-drilled microvias.",
        "Un-drillable holes / broken drill bits."),
    "drill_aspect_ratio": Remediation(
        "Reduce board thickness or enlarge the drill to lower depth-to-diameter.",
        "Plating voids / thin barrel walls → reliability failures."),
    "drill_to_drill_spacing": Remediation(
        "Increase hole-to-hole spacing.",
        "Wall breakout between adjacent holes."),
    "drill_wander_budget": Remediation(
        "Add annular margin for drill wander or tighten the hole callouts.",
        "Registration breakout on production drilling."),
    "backdrill_stub_length": Remediation(
        "Adjust the backdrill depth to shorten the residual via stub.",
        "Stub resonance degrades high-speed signal integrity."),
    "via_to_copper_clearance": Remediation(
        "Increase the via-to-copper (antipad) clearance.",
        "Shorts to adjacent copper on registration error."),
    "via_tenting": Remediation(
        "Tent or plug the via per the fab's capability.",
        "Solder wicking / exposed via → assembly defects."),
    "via_in_pad_thermal_balance": Remediation(
        "Fill and cap via-in-pad, or add thermal relief.",
        "Solder voiding / opens under the component."),
    "unsupported_hole_types": Remediation(
        "Replace unsupported hole types (e.g. blind/buried) with a supported stackup.",
        "Fab cannot build the board as drawn (CAM hold)."),
    "min_slot_width": Remediation(
        "Widen routed slots to the fab's minimum routing tool.",
        "Slot cannot be milled."),
    "layer_registration_margin": Remediation(
        "Add annular/copper margin to absorb layer-to-layer registration.",
        "Inner-layer breakout on a deep stack."),
    "plating_uniformity": Remediation(
        "Narrow the range of hole sizes or reduce the maximum aspect ratio.",
        "Uneven barrel plating → field reliability risk."),

    # --- solder mask / silkscreen -----------------------------------------
    "solder_mask_expansion": Remediation(
        "Set mask expansion to the fab's rule (typically ~0.05 mm).",
        "Mask slivers or exposed copper around pads."),
    "solder_mask_web": Remediation(
        "Widen the mask web between openings, or merge the openings.",
        "The mask web breaks off → solder bridging."),
    "mask_to_trace_clearance": Remediation(
        "Increase the mask-opening to adjacent-trace clearance.",
        "Exposed adjacent copper → shorts."),
    "silkscreen_min_width": Remediation(
        "Thicken silkscreen strokes/text to the fab minimum.",
        "Illegible or missing legend."),
    "silkscreen_on_copper": Remediation(
        "Move silkscreen off exposed copper and pads.",
        "Silk on pads → poor solderability."),
    "silkscreen_over_mask_defined_pads": Remediation(
        "Keep silk clear of mask-defined pad openings.",
        "Silk in the pad opening → assembly defects."),
    "silkscreen_clearance": Remediation(
        "Pull silkscreen back from the board edge and drilled holes.",
        "Silk milled/drilled away or smeared."),
    "aperture_definition_errors": Remediation(
        "Fix or define the offending apertures in the Gerber output.",
        "Ambiguous artwork → CAM hold / misfabrication."),

    # --- fabrication / stackup --------------------------------------------
    "dielectric_thickness_uniformity": Remediation(
        "Even out dielectric thicknesses or choose a symmetric stackup.",
        "Impedance drift and board warp."),

    # --- mechanical / outline / thermal -----------------------------------
    "fillet_radius_milling": Remediation(
        "Add internal corner radii ≥ the router bit radius.",
        "Sharp inside corners cannot be milled as drawn."),
    "copper_thermal_area": Remediation(
        "Add thermal relief or reduce copper mass on the thermal pad.",
        "Cold joints / tombstoning at reflow."),
    "tab_routing_mousebites": Remediation(
        "Tune mouse-bite/tab spacing and hole size for a clean depanel.",
        "Rough break-off edges and board stress."),
    "missing_tooling_holes": Remediation(
        "Add the fab's required tooling/fiducial holes.",
        "Panelization / assembly registration problems."),
    "plane_fragmentation": Remediation(
        "Reconnect fragmented plane islands (stitch or reroute).",
        "Isolated copper and interrupted return paths."),
    "thermal_relief_spoke_width": Remediation(
        "Widen thermal-relief spokes for solderability and current.",
        "Cold joints or insufficient current capacity."),

    # --- assembly / DFA ----------------------------------------------------
    "component_to_component_spacing": Remediation(
        "Increase courtyard spacing between components.",
        "Assembly collisions and rework."),
    "solder_paste_area_coverage": Remediation(
        "Adjust the paste aperture area ratio for reliable paste release.",
        "Insufficient or excess paste → solder defects."),
    "tombstoning_risk": Remediation(
        "Balance copper/thermal mass between the passive's two pads "
        "(add relief to the heavier pad).",
        "Tombstoning → open joints."),
    "wave_solder_shadowing": Remediation(
        "Reorient/space through-hole parts so tall neighbors don't shadow them "
        "along the wave-travel direction (or hand-solder the shadowed pins).",
        "Cold / incomplete through-hole joints from the wave."),
    "polarity_marking_consistency": Remediation(
        "Add a silkscreen polarity / pin-1 marker beside each polarized part.",
        "Reversed-polarity assembly → dead or damaged boards."),

    # --- high-speed SI -----------------------------------------------------
    "impedance_control": Remediation(
        "Adjust trace width or stackup to hit the target impedance.",
        "Reflections / eye closure on high-speed nets."),
    "diff_pair_spacing": Remediation(
        "Hold a constant intra-pair gap along the coupled length.",
        "Mode conversion and skew → SI loss."),
    "diff_pair_skew": Remediation(
        "Length-match the pair (serpentine the shorter member).",
        "Skew → common-mode noise / EMI."),
    "return_path_interruptions": Remediation(
        "Reroute the trace or add stitching so the reference plane is continuous under it.",
        "EMI and SI degradation from the return-current detour."),
    "crosstalk_estimate": Remediation(
        "Increase spacing between sensitive nets or add a guard/ground trace.",
        "Crosstalk-induced noise / bit errors."),
}


def remediation_for(check_id: str) -> Optional[Remediation]:
    """Guidance for a check id, or None if none is registered."""
    return GUIDANCE.get(check_id)
