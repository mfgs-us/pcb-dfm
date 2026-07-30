# Cutout check gaps — spec

Three gaps around board cutouts, found by asking whether anything checks the
components that *require* one.

Cutouts themselves are modelled well. `outline_contours_mm`
(`gerber_backend.py:529`) assembles the outline layer's closed contours and
states the convention: the largest-area contour is the board boundary, and
"smaller closed contours are internal cutouts and slots, which are also real
edges". Five checks consume them — `trace_over_cutout`, `copper_to_edge_distance`
(which explicitly measures clearance to internal cutouts and slots),
`min_slot_width`, `fillet_radius_milling`, `board_outline_continuity`.

Every one of those relates a cutout to **copper or the outline**. Nothing relates
a cutout to a **component**, in either direction:

- no check asks whether a part that needs a cutout actually got one (§1),
- no check asks whether anything is sitting in a cutout that should not be (§2),
- and the component-facing edge checks cannot see cutouts at all (§3).

Conventions (same as `new_check_domains.md` and `stackup_check_domains.md`):
every check is `not_applicable` without its inputs, states a metric with
target/limit, and follows the tier rules. No folklore — objective, sourced rules
only, which is why §4 below is spec'd as a non-goal rather than a check.

**Status (2026-07):** all three spec'd, filed (#105–#107) and **built**.

---

## 1. Footprint declares a cutout the board does not have — `component_cutout_present`  [#105] [DONE]

**Why:** a mid-mount USB-C, an SD-card holder, a buzzer, a recessed connector —
each needs a milled opening, and the requirement travels *with the footprint*: in
KiCad the footprint draws its required cutout on `Edge.Cuts` inside its own
graphics. Forgetting to propagate that into the board outline is silent at every
stage we currently check. The artwork is legal, the copper is legal, the outline
is a valid closed contour — and the boards arrive with no opening, the connector
will not seat, and the lot is scrap.

No library knowledge is needed to catch it, which is what makes this worth
building: **the footprint states its own requirement.**

**Inputs — the data is already parsed and thrown away.** `_courtyard_hull`
(`kicad.py:405`) walks each footprint's `fp_line` / `fp_rect` / `fp_circle` /
`fp_poly` graphics and discards everything that fails its `on_crtyd(g)` filter.
Widening that to also collect `Edge.Cuts` graphics per footprint yields the
required cutout; the rotate-and-translate to board coordinates is the same
transform the courtyard already gets (`kicad.py:416-423`).

**Two implementation traps, both already visible in the existing code:**

- **Do not convex-hull it.** `_courtyard_hull` returns a hull because a keep-out
  only needs to be conservative. A cutout is a real polygon: it can be concave (an
  L-shaped or notched opening), and one footprint can declare *several* separate
  cutouts. Collect closed contours, not a point cloud.
- **Y-flip.** `_courtyard_hull` is documented as working "in the KiCad frame (Y
  flipped to Gerber later, with pads)", and that flip happens at
  `kicad.py:572-582`, where pads and `courtyard` are each mirrored. A cutout
  polygon has to be flipped in the same block or it lands mirrored about the
  board's X axis — and on a symmetric board it will look almost right.

**Rule:** for each cutout a footprint declares, is there a matching interior
contour in the board outline at that location (centroid inside, and area within a
tolerance)? A declared cutout with no match fails: the milling was not propagated.

**One-directional, deliberately.** A board cutout with no component asking for it
is *not* a defect — ventilation, mechanical clearance, mounting, antenna
keep-outs are all legitimate. Only the missing direction is a finding.

**Metric:** count of declared-but-absent cutouts (dimensionless, target 0).
**Category:** `mechanical_outline`. **Severity:** `error` — an unbuildable
assembly, not a margin.

**Availability:** KiCad carries footprint `Edge.Cuts`; ODB++ has its rout layer;
bare Gerbers carry the cutout but no component-requirement data, so they report
`not_applicable`. A 🔬 data-gated check.

---

## 2. Pads over a cutout — `pad_over_cutout`  [#106] [DONE]

**Why:** `trace_over_cutout` covers traces crossing an internal void. A *pad* over
a void is the same defect and is not covered: there is nothing to solder to, and
the pad's copper is unsupported at the milled edge where it is most likely to
lift.

**Inputs:** `Component.pads` already carry absolute board geometry — `x_mm`,
`y_mm`, `width_mm`, `height_mm`, `rotation_deg`, `shape` (`design_model.py:224`)
— plus the interior contours from `outline_contours_mm`. **No ingest change
needed;** this is buildable today, unlike §1.

**Rule:** a pad whose copper extent overlaps an interior cutout contour is a
finding. Reuse the point-in-polygon and segment-crossing helpers
`trace_over_cutout` already uses (`_point_in_polygon`, `segments_cross`) rather
than adding a second geometry path.

**The discriminator that makes or breaks this check:** a component *body* over a
cutout is frequently correct — that is precisely what "mid-mount" means, and a
buzzer or connector is *supposed* to sit in its opening. So:

- restrict this to **pads**, never courtyards or bodies, because a pad over a
  void is never intentional; and
- when §1 lands, a pad inside a cutout that its own footprint declared is
  intentional by construction and must be excluded.

Getting this wrong means flagging every correctly designed mid-mount connector,
so the pad-only scope is not a simplification — it is the check.

**Metric:** count of pads overlapping a cutout (dimensionless, target 0).
**Category:** `mechanical_outline`. **Severity:** `warning` until validated on
real boards with cutouts, then reconsider.

---

## 3. Component edge checks cannot see cutouts — `tall_part_edge_clearance` / `component_edge_clearance` fix  [#107] [DONE]

**Why:** an internal cutout is a real board edge — `copper_to_edge_distance` and
`trace_over_cutout` both treat it as one. The component-facing checks do not, and
they miss it in two different ways:

| Check | Edge source | Blind to |
|---|---|---|
| `tall_part_edge_clearance` (and `outline_sharp_corners`, `silkscreen_off_board`) | `board_contour_verts` (`_design_advisory.py:100`) — the **largest** contour only | interior cutouts |
| `component_edge_clearance` | `queries.get_board_bounds` (`impl_component_edge_clearance.py:27`) — a **bounding box** | interior cutouts *and* any concave boundary |

So a tall part standing at the lip of a cutout has no clearance check at all, and
on a non-rectangular board `component_edge_clearance` measures to a box the board
does not occupy. The second is the more surprising of the two: a part sitting in a
notch or outside an L-shaped boundary reads as comfortably interior.

**Fix:** give `board_contour_verts` a companion that returns the interior
contours, measure component clearance against *all* edges (boundary + cutouts),
and move `component_edge_clearance` off the bounding box onto the real contour.

**Guard:** parts that legitimately sit in or over a cutout (§2's mid-mount case)
must not become false positives here. Sequence this after §1 so the
"intentionally in its own cutout" exclusion exists to reuse, or scope the first
cut to cutouts no footprint declared.

**Metric:** unchanged for both checks (mm to the nearest edge). **Category:**
unchanged. Expect baseline movement on any corpus board with a cutout or a
non-rectangular outline — that movement is the point, so review each diff rather
than blessing it.

---

## 4. Non-goal: inferring "this part needs a cutout" from its footprint name

Recognising mid-mount USB-C, SD holders and similar from library/footprint
strings would extend §1 to sources that carry no footprint `Edge.Cuts`. It is
deliberately **not** specified.

`classify_component` (`design_intel.py:119`) does infer from footprint strings,
but only coarse classes (capacitor, resistor, LED, diode) where a wrong guess
costs little. Here a wrong guess tells someone their board is missing milling when
it is not — on any board using a through-hole or edge-mount variant of a
similarly-named part. That is the folklore line this project holds, and §1 does
not need to cross it: the footprint already states the requirement, objectively,
for the sources that matter.
