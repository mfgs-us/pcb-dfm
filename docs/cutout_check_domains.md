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

## 1. Artwork missing a cutout the design declares — `component_cutout_present`  [#105] [DONE, RESCOPED]

**Originally spec'd as** "a footprint declares a cutout and the board outline
never got it — the milling was not propagated". **That premise was wrong**, and
testing the built check against a real board (droyd-wireless-umi-revmin) is what
showed it.

**What was measured.** Adding an `Edge.Cuts` rect *inside* a footprint and
plotting the board takes the outline layer from one closed contour to two, the
second at exactly the footprint's position:

```
board.kicad_pcb            closed contours: 1   (20..112 x -40..-75)
+ footprint Edge.Cuts      closed contours: 2   (+ 71.20..72.80 x -54.00..-56.00)
```

**KiCad plots a footprint's Edge.Cuts graphics straight into the board outline
layer.** There is no propagation step for a designer to forget. When design data
and artwork come from the same board file — the default path for a KiCad input —
this check passes trivially and always.

**What it does catch**, verified: old Gerbers paired with an updated board file
reports `fail` (`D1 at (72.0, -55.0)`). A fab package out of sync with the design
it is sent alongside is a real and expensive failure — the fab mills the outline
it was given — and it is invisible to every other check because both halves are
internally valid. That is a narrower subject than originally claimed, and the
check, its JSON, its remediation and this section now say so.

**What it cannot catch, by construction:** a footprint that *should* declare an
opening and does not. See §5 — that is the case a real board actually had.

**Inputs:** `Component.required_cutouts`, collected by `_footprint_cutouts`
(`kicad.py`) from each footprint's Edge.Cuts graphics, versus the interior
contours of the supplied artwork. Two things the collector does deliberately
differently from the courtyard beside it:

- **no convex hull** — an opening can be concave (L-shaped, notched), and a hull
  would claim milled area that is not milled;
- **one entry per closed contour** — a footprint may declare several separate
  openings, and loose `fp_line`s that do not close are dimension or centre marks,
  not openings.

Cutouts ride the same Y-flip as pads and courtyard (`_to_gerber_frame`); missing
it lands them mirrored about X, which on a symmetric board looks almost right.

**Rule:** centroid containment plus an area-ratio bound, not shape equality — an
opening milled slightly larger, or with rounded router corners, is still the right
opening.

**One-directional, deliberately.** A board cutout with no component asking for it
is *not* a defect — ventilation, mechanical clearance, mounting and antenna
keep-outs are all legitimate.

**Metric:** count of declared-but-absent cutouts (target 0). **Category:**
`mechanical_outline`. **Severity:** `error` — the wrong outline gets milled.

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

## 4. Non-goal: inferring a cutout requirement from a footprint *name*

Recognising mid-mount USB-C, SD holders and similar from library/footprint name
substrings. Deliberately **not** specified.

`classify_component` (`design_intel.py:119`) does infer from footprint strings,
but only coarse classes (capacitor, resistor, LED, diode) where a wrong guess
costs little. A wrong guess here tells someone their board is missing milling when
it is not — on any board using a through-hole or edge-mount variant of a
similarly-named part.

## 5. Open: the case a real board actually had — part-level cutout requirements

`droyd-wireless-umi-revmin` places `droyd:LED_SK6812MINI-E`, a reverse-mount LED
that emits *through* the board and needs a window milled under it. The board has
no window. The footprint declares no cutout — only a courtyard, silk and four pads
— so §1 is blind to it, and §4 rules out guessing from the name.

Neither the artwork nor the design data says a window is required: it is a fact
about the *part*. Two ways to close it, and they are not equivalent:

1. **Fix the library footprint.** Draw the window on Edge.Cuts in
   `droyd:LED_SK6812MINI-E`. KiCad then mills it on every board using that
   footprint, and no check is needed — the requirement becomes geometry. This is
   the right fix for the boards that exist today.
2. **An MPN-keyed rule table** (would need its own issue). Distinct from §4 in the
   way that matters: an exact manufacturer-part-number match with a datasheet
   citation per entry is a sourced fact, not a name heuristic — and a custom
   footprint name like `LED_SK6812MINI-E` carries the MPN literally. It would
   catch this class *before* someone remembers to fix the library, at the cost of
   a table that only covers the parts in it, and it needs a BOM or a footprint
   name carrying the MPN.

Worth recording from the same experiment: once that window exists on revmin,
`copper_to_edge_distance` and `fillet_radius_milling` both fail — there is copper
hard against where the opening would be milled. The library fix is not free.
