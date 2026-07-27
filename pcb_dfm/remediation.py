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
    "copper_balance_plating": Remediation(
        "Balance the two outer layers (add copper thieving/​hatch to the lighter "
        "side) so top and bottom coverage are comparable.",
        "Reflow warp and uneven outer-layer plating."),
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
    "microvia_geometry": Remediation(
        "Keep microvia aspect ratio ≤ 1:1 (target 0.75:1): enlarge the drill, "
        "thin the dielectric, or split a multi-dielectric span into stacked/​"
        "staggered microvias.",
        "Microvia plating voids / open connections → field failures."),
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
    "npth_to_copper_clearance": Remediation(
        "Pull copper back from non-plated (mounting/tooling) holes to the fab's "
        "keep-out (typically ≥ 0.25 mm).",
        "The bare drilled wall can nick/lift adjacent copper or short to a "
        "standoff — no plated barrel protects it."),
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
    "stackup_symmetry": Remediation(
        "Mirror the layer construction (copper weights and dielectric "
        "thicknesses) about the board mid-plane.",
        "An unbalanced stackup warps on reflow → fab reject/re-quote and "
        "assembly coplanarity problems."),

    # --- mechanical / outline / thermal -----------------------------------
    "board_outline_continuity": Remediation(
        "Join the board outline into a single closed loop (close the gap between "
        "the dangling endpoints on the outline/Edge.Cuts layer).",
        "An open profile has no determinable board boundary → the fab cannot rout "
        "the board (hard reject)."),
    "fillet_radius_milling": Remediation(
        "Add internal corner radii ≥ the router bit radius.",
        "Sharp inside corners cannot be milled as drawn."),
    "castellated_edge_plating": Remediation(
        "Route the board edge through (or just inside) the castellation centre so "
        "at least half the plated barrel stays in copper, and keep castellation "
        "pitch ≥ the fab's minimum (~1 mm).",
        "Sliver/​breaking-out edge plating and bridged castellations → scrap."),
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
    "stencil_aperture_ratio": Remediation(
        "Raise the IPC-7525 area ratio (≥ 0.66): enlarge the aperture, use a "
        "thinner stencil foil, or a step-down/electropolished stencil for the "
        "fine-pitch openings.",
        "Paste won't release from the stencil → insufficient/​skipped joints."),
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
    "highspeed_stub_length": Remediation(
        "Remove the dangling branch / route the high-speed net point-to-point "
        "(or back-drill the offending via).",
        "Stub reflections degrade the signal / close the eye."),

    # --- design advisory (objective layout-quality; advisory, never a fab reject) ----
    "outline_sharp_corners": Remediation(
        "Chamfer or round acute outer board corners.",
        "Sharp outer spikes snag in handling and chip at the point."),
    "floating_copper": Remediation(
        "Delete the isolated copper, or connect/ground it if intentional.",
        "Unconnected copper floats → antenna / EMI coupling, or signals a routing error."),
    "silkscreen_off_board": Remediation(
        "Pull silkscreen inside the board-edge keep-out.",
        "Silk crossing the outline is trimmed at depanel → missing/illegible legend."),
    "fiducial_coverage": Remediation(
        "Add ≥ 3 non-collinear global fiducials (and local fiducials at fine-pitch parts).",
        "Pick-and-place can't optically align the board → placement offset."),
    "reference_designator_coverage": Remediation(
        "Add a silkscreen reference designator near every component.",
        "Assembly / rework can't identify unlabeled parts."),
    "component_edge_clearance": Remediation(
        "Move edge-hugging components inward from the board edge.",
        "Depaneling stress and handling damage to parts at the edge."),
    "test_point_coverage": Remediation(
        "Add a test point (or expose a via) on each untestable net.",
        "ICT / flying-probe can't verify a net with no probe-accessible point."),
    "antenna_keepout": Remediation(
        "Clear copper out of the antenna/RF keep-out region (pour, traces, plane).",
        "Copper under/near the antenna detunes it and cuts wireless range."),
    "teardrop_presence": Remediation(
        "Add teardrops where thin traces meet small-annular vias.",
        "Drill wander breaks out of a thin annular ring without the teardrop's extra copper."),
    "unconnected_pads": Remediation(
        "Route (or intentionally no-connect) each pad that resolves to no net.",
        "An unrouted / disconnected pin is a functional defect that survives to the fab."),
    "power_feed_robustness": Remediation(
        "Stitch redundant vias at power/ground layer transitions.",
        "A single layer-transition via is a current and reliability single-point-of-failure."),
    "decoupling_proximity": Remediation(
        "Move decoupling caps right up to the IC power pin they serve.",
        "Loop inductance to a distant bypass cap negates its decoupling."),
    "decoupling_adequacy": Remediation(
        "Add at least one bypass capacitor (typ. 0.1 µF) from each IC supply rail "
        "to ground, close to the pin.",
        "An undecoupled supply rail → noise, brown-out and marginal behaviour."),
    # --- electrical design review ------------------------------------------
    "floating_or_single_pin_net": Remediation(
        "Finish or delete the net: connect its second endpoint, or mark it a "
        "deliberate no-connect.",
        "A single-pin net is a stub or forgotten connection."),
    "unpowered_ic": Remediation(
        "Tie every IC to its ground (and power) rail, including the exposed pad.",
        "An IC with no ground/power connection will not function."),
    "crystal_load_caps": Remediation(
        "Add the load capacitor to ground on each oscillator pin (value per the "
        "crystal's CL spec).",
        "A crystal without load caps won't start or will run off-frequency."),
    "led_series_resistor": Remediation(
        "Add a series current-limit resistor (or use a constant-current driver).",
        "An LED across a rail draws unlimited current → burnout."),
    "i2c_pullup_presence": Remediation(
        "Add a pull-up resistor from each I2C SDA/SCL line to its bus rail.",
        "Open-drain I2C won't idle high without pull-ups → dead bus."),
    "reset_pullup_presence": Remediation(
        "Give the reset net a defined idle level (pull-up, RC, or a supervisor).",
        "A floating reset causes spurious or missed resets."),
    "bulk_capacitance_present": Remediation(
        "Add a bulk capacitor (≥ 1 µF, typ. 10 µF) from the supply to ground.",
        "No bulk energy storage → rail sags on load transients."),
    "differential_pair_completeness": Remediation(
        "Route (or add) the missing member of the differential pair.",
        "A half-routed pair breaks the differential signal."),
    "debug_port_test_access": Remediation(
        "Bring SWD/JTAG lines to a test point or programming header.",
        "No debug access makes bring-up and field reflash impossible."),
    # --- placement / consistency design review -----------------------------
    "decoupling_same_side": Remediation(
        "Place the bypass cap on the IC's side next to the pin (or directly "
        "under it on the back), not far away on the opposite side.",
        "A long via loop to the cap negates its decoupling."),
    "crystal_proximity": Remediation(
        "Move the crystal and its load caps right next to the IC's oscillator pins.",
        "A long crystal loop picks up noise and can stop oscillation."),
    "tall_part_edge_clearance": Remediation(
        "Pull tall parts back from the board edge (or confirm the enclosure clears them).",
        "A tall body at the edge fouls the case or a neighbouring board."),
    "duplicate_refdes": Remediation(
        "Give each placed component a unique reference designator.",
        "Duplicate refdes breaks BOM/pick-and-place matching."),
    "rail_name_aliasing": Remediation(
        "Merge the aliased power nets into one name (or confirm they are truly separate rails).",
        "A rail split by a naming slip can leave part of it unconnected."),
    "polarized_orientation_consistency": Remediation(
        "Check the odd-angle polarized part against its neighbours; fix a flipped placement.",
        "A reversed polarized part fails or is destroyed at power-up."),
    "mounting_hole_keepout": Remediation(
        "Move components out of the mounting hole's screw-head / standoff keep-out.",
        "A part under the mounting hardware collides with it at assembly."),
    "fine_pitch_fiducials": Remediation(
        "Add a local fiducial pair near each fine-pitch / BGA part.",
        "Global fiducials alone cannot correct local placement error at fine pitch."),
    "power_ground_trace_width": Remediation(
        "Widen power/ground rails to at least the signal trace width.",
        "A rail narrower than the signals it feeds is a current and IR-drop bottleneck."),
    "courtyard_overlap": Remediation(
        "Space same-side components so their courtyards no longer overlap.",
        "Overlapping courtyards are too close to place, inspect, or rework."),
}


def remediation_for(check_id: str) -> Optional[Remediation]:
    """Guidance for a check id, or None if none is registered."""
    return GUIDANCE.get(check_id)
