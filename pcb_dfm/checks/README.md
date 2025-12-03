# PCB-DFM Check Implementation Tracker

## assembly
- [x] component_to_component_spacing
- [ ] polarity_marking_consistency
- [ ] solder_paste_area_coverage
- [ ] tombstoning_risk
- [ ] wave_solder_shadowing

## copper_geometry
- [x] acid_trap_angle
- [x] copper_density_balance
- [x] copper_sliver_width
- [x] min_annular_ring
- [x] min_trace_spacing
- [x] min_trace_width

## drill_via_integrity
- [ ] backdrill_stub_length
- [x] drill_aspect_ratio
- [x] drill_to_drill_spacing
- [x] min_drill_size
- [x] via_tenting
- [x] via_to_copper_clearance

## fab_process_compatibility
- [x] aperture_definition_errors
- [x] missing_tooling_holes
- [x] silkscreen_over_mask_defined_pads
- [ ] unsupported_hole_types

## fabrication_stackup
- [x] copper_to_edge_distance
- [ ] dielectric_thickness_uniformity
- [ ] impedance_control
- [ ] layer_registration_margin

## high_speed_si
- [ ] crosstalk_estimate
- [ ] diff_pair_skew
- [ ] diff_pair_spacing
- [ ] highspeed_stub_length
- [ ] return_path_interruptions

## mechanical_outline
- [ ] fillet_radius_milling
- [ ] min_slot_width
- [ ] tab_routing_mousebites

## solder_mask_silkscreen
- [x] mask_to_trace_clearance
- [x] silkscreen_min_width
- [x] silkscreen_on_copper
- [x] solder_mask_expansion
- [x] solder_mask_web

## thermal_power
- [x] copper_thermal_area
- [ ] plane_fragmentation
- [~] thermal_relief_spoke_width
- [x] via_in_pad_thermal_balance

## yield_prediction
- [ ] drill_wander_budget
- [ ] etch_compensation_margin
- [ ] plating_uniformity
