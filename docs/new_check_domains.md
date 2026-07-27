# New DFM check domains — spec

Seven domains beyond the current catalogue. Ordered by value/effort.

**Status (2026-07):** the three geometry-buildable domains are shipped and
validated on the droyd board + golden corpus with zero false positives —
§1 `stencil_aperture_ratio` (#56), §2 `castellated_edge_plating` (#57),
§3 `copper_balance_plating` (#58). The remaining domains (§4 IPC-2152 current,
§5 flex, §6 HDI/microvia) are **paused**: each needs a design-data input no
current source carries (per-net current, bend regions, microvia spans), so a
check built now would be `not_applicable` on every real board and validatable
only synthetically. They stay spec'd here and will be built when a real
HDI/flex/current-annotated board is available to validate against — the same
"validate on real geometry before merge" bar the first three met.

Conventions: every check is `not_applicable` without its inputs, states a metric
with target/limit, and follows the tier rules (design-advisory never hard-fails;
fab/assembly may). No folklore — objective, sourced rules only.

---

## 1. Stencil aperture ratios (IPC-7525) — `stencil_aperture_ratio`  [DONE #56]
**Why:** paste release from the stencil is governed by two ratios; below them the
paste stays in the aperture and you get insufficient/​skipped joints. Definitive.

**Inputs:** a solder-paste layer (`*.gtp`/`*.gbp`/name contains "paste") →
per-aperture polygons; stencil foil thickness (design-data `stencil_thickness_mm`,
default 0.12 mm = ~5 mil).

**Rules (IPC-7525):**
- **Area ratio** `AR = area / (perimeter × t)` should be **≥ 0.66** (warn `< 0.66`,
  fail `< 0.5`). This is the dominant release predictor for small apertures.
- **Aspect ratio** `= min(width,height) / t` should be **≥ 1.5** (for slots).
- Report the worst aperture and the count below target.

**Metric:** min area ratio (dimensionless). **Category:** `fab_process_compatibility`.
Exclude board-scale polygons (a paste "aperture" the size of a thermal pad is a
windowpane; only aperture-scale features count).

## 2. Castellated / edge plating — `castellated_edge_plating`  [DONE #57]
**Why:** castellated modules (half-vias on the board edge) and edge-plated boards
have specific fab rules; a plated hole that only *touches* the edge (not bisected)
or copper hard against an unplated edge fails.

**Inputs:** plated drills + board outline (both present). A plated hole whose
centre is within ~½ its diameter of the outline is a castellation.

**Rules:**
- A castellation drill should be **bisected** by the edge (centre on/near the
  outline ± tolerance), not merely tangent — a tangent plated hole breaks out.
- Castellation **pitch** (edge-to-edge along the edge) ≥ a floor (default 1.0 mm).
- Copper-to-unplated-edge is already `copper_to_edge_distance`; this adds the
  castellation-specific geometry.

**Metric:** count of malformed castellations. **Category:** `mechanical_outline`.
N/A when no plated hole sits on the outline.

## 3. Per-layer copper balance for plating — `copper_balance_plating`  [DONE #58]
**Why:** grossly unequal copper *coverage* between layers plates unevenly (dog-bone
/ dimple), and an unbalanced outer/inner set warps on reflow. Distinct from the
existing *local* `copper_density_balance`; this is whole-layer coverage %.

**Inputs:** ≥ 2 copper layers.
**Rule:** coverage% per layer = copper area / board area; flag when the spread
(max − min) across layers exceeds a limit (warn > 30 pp, fail > 50 pp), or when the
two outer layers differ by > 20 pp (warp risk). **Metric:** coverage spread (pp).
**Category:** `fabrication_stackup`.

---

## 4. IPC-2152 current capacity — `trace_current_capacity`  [PAUSED — needs current spec + test board]
**Why:** a trace narrower than its rated current needs will overheat. Turns the
heuristic `copper_thermal_area` into a real calc.

**Inputs:** per-net **rated current** (design-data, new `current_a` on a net/spec)
+ routed width + copper thickness (+ internal/external layer flag).
**Rule (IPC-2152 curve fit):** required cross-section
`A_mils2 = (I / (k·ΔT^0.44))^(1/0.725)` (k = 0.024 internal / 0.048 external,
ΔT default 10 °C); required width = A / (t·1.378). Compare to the net's *narrowest*
routed width; warn/fail on deficit. **Category:** `thermal_power`. N/A without a
current spec.

## 5. Flex / rigid-flex — `flex_bend_rules`  [PAUSED — needs flex-region data + test board]
**Why:** a whole domain we don't touch. High effort, only for flex builds.
**Inputs:** a designated **flex/bend region** (design-data keepout kind `bend`) +
stackup.
**Rules:** bend radius ≥ 10× flex thickness (1-layer) / 20× (2-layer); **no
vias/pads inside a bend zone**; traces cross the bend perpendicular and use arcs,
not right-angles; teardrops at the rigid↔flex transition. **Category:** new
`flex` or `mechanical_outline`. N/A without a bend region.

## 6. HDI / microvia — `microvia_geometry`  [PAUSED — has a KiCad data path; needs an HDI test board]
**Why:** microvias have tighter aspect and stacking rules than through-holes.
**Inputs:** blind/buried via data (from/to layer + drill) — KiCad `(via (blind))`
/ IPC-2581.
**Rules:** microvia **aspect ratio** (depth/diameter) ≤ 0.75:1 (fail > 1:1);
**stacked** microvias flagged for fill/planarisation review; target-pad capture.
**Category:** `drill_via_integrity`. N/A without blind/buried via data.

## 7. (extension) Paste coverage vs pad already exists (`solder_paste_area_coverage`);
`stencil_aperture_ratio` (§1) is the release-side complement.

---

## Sequencing
Build §1 (stencil, cleanest definitive win) → §2 (castellated) → §3 (copper
balance) — all from existing geometry. Then §4/§6 when a current/​via-topology
design-data field lands, and §5 as a dedicated flex effort.
