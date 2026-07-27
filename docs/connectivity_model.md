# Net connectivity model — status and completion plan

The net-aware checks (`via_to_copper_clearance`, `min_trace_spacing`,
`solder_mask_web`, `copper_sliver_width`, `diff_pair_spacing`,
`return_path_interruptions`, `crosstalk_estimate`, `tombstoning_risk`) all rest
on one question: **is this copper the via/trace's own net, or a foreign net?**
That is answered by `geometry/net_map.py`, which labels each copper polygon with
a net. The more copper it labels, the more of those checks can be *definitive*
(hard pass/fail) instead of *advisory*.

## Where it stands

Coverage measured as `tagged_polygon_count / total copper polygons`, **in the
real pipeline (netlist registered to the board first)**:

| Board | Design source | Seeding | Coverage |
|---|---|---|---|
| droyd | KiCad | routed segments + points | **75%** |
| pcbtools | IPC-D-356 | access points only | **97%** |

> ⚠️ A long investigation chased a "pcbtools 3%" residual that **did not exist**.
> The 3% came from calling `build_net_map` directly in diagnostic scripts, which
> skips the engine's `_auto_register_netlist` step. An IPC-D-356 netlist states
> coordinates in the CAD tool's frame (here offset (0.76, 7.78) mm from the
> Gerber origin); un-registered, its points land off-copper or in the wrong
> polygons, which *looks* like a catastrophic over-merge. Registered — as every
> real check run does — coverage is **97%**. Always measure through the engine
> path, or apply `register_to_board` first. §2 below documents the (real, but
> now moot) labelling analysis; §4's coverage guard exists to catch this class of
> regression.

The model is a union-find: seed polygons from the netlist (points that fall in a
polygon, segments that hit one), union copper that is **one conductor**, then
label each conductor from its seed. A group holding two different net labels is
discarded as ambiguous (safe: guessing there would mislabel copper and
manufacture a false short/open).

### What the "safe sub-win" fixed (done)

`_polygons_touch` used to treat two polygons as one conductor if **any vertex of
one lay inside the other's outline**. A ground pour is a single polygon whose
outline snakes around the traces it clears, so every trace sitting in a clearance
had its vertices "inside" the pour and was merged into the pour's net — on droyd
this collapsed ~13 nets into one ambiguous blob that was then discarded.

It is now an **edge test**: two polygons are one conductor only where their
copper actually meets (edges intersect or abut within 10 µm). Result: droyd
coverage 69% → 75%, fewer false merges, and ~5× faster (it no longer point-tests
against multi-thousand-vertex pour outlines). No corpus baseline moved.

## Why it is still capped (the real ceiling)

The residual — and the entire pcbtools 3% — traces to **copper polygons not
modelling their interior clearance holes**. gerbonara hands us a pour as one
`ArcPoly` whose outline winds around every clearance (2851 vertices on droyd's
GND), but `geometry/primitives.Polygon` keeps only a flat vertex list
(`"Later you can attach holes"` — never done). Two consequences:

1. **Seeding leaks.** A netlist point that lands in a pour's *clearance* is still
   inside the pour's outline, so it seeds the pour with a foreign net. On a big
   ground pour the correct-net votes dominate, but on a points-only board
   (IPC-D-356, sparse seeds) the spurious votes are enough to poison labels.
2. **The antipad case.** A via passing through a plane sits in the plane's
   antipad (a hole). With no hole modelled, the via tests as *inside the plane
   copper* → `via_to_copper_clearance` reports 0 mm to a plane it does not touch.

Both are the same missing primitive: **polygon interior rings (holes) + a
hole-aware point-in-polygon.**

## Completion plan

### 1. Model polygon holes (the keystone — also the "antipad/plane" work item)
**Partly done (#49):** clear-polarity (LPC) antipads/cut-outs are now folded into
the copper as holes — `Polygon.holes` + hole-aware `contains_point`,
`gerber_polygons_mm` separates dark/clear and attaches clears as holes, and net-map
seeding/bridges + via-to-copper are hole-aware. This fixed the confirmed antipad
false-0 (a via in an LPC antipad now reads its real clearance). **Still open:** the
KiCad *keyhole* case below (holes woven into a self-winding region, not separate
LPC objects) — see §2 for why that is the real points-only residual.

- Extend `Polygon` with `holes: List[List[Point2D]]` (interior rings), default
  empty so every existing call site is unchanged. *(done)*
- In `geometry/gerber_backend._object_polygons_mm`, split each `ArcPoly` into its
  **exterior ring and interior rings** instead of flattening the winding outline
  to one vertex list. gerbonara's arc-poly winding + polarity gives the ring
  nesting; the standard rule is *even-depth rings are copper, odd-depth are
  holes*. (KiCad exports pours as a single self-winding region, so this is a
  ring-nesting / point-in-ring depth computation, not separate objects.)
- Make `_point_in_polygon` (and the mask/annular copies of it) hole-aware: inside
  the exterior **and** outside every hole. Add `polygon_contains(poly, x, y)`.
- Payoffs, all at once:
  - **Seeding** stops leaking into pours → points-only coverage climbs.
  - **`via_to_copper_clearance`** stops the false 0 mm inside planes (antipad).
  - **Merge** precision improves further (a trace in a clearance is provably not
    the pour's copper).

### 2. Net-aware union for the points-only residual — INVESTIGATED, NO SAFE WIN
This was prototyped and measured; it does **not** yield a safe coverage gain, and
the reason is worth recording so it isn't re-attempted.

- A *safe* flood ("label P iff every seed reachable from P is net N") is provably
  identical to the current connected-component labelling: in a connected
  component every polygon reaches every seed, so both discard an over-merged
  multi-net component wholesale. Measured on pcbtools and droyd: safe-flood
  coverage == current, to the polygon.
- A *label-respecting* union ("never merge two different-net seeds") changed
  nothing either — the over-merge does not run through direct seed↔seed edges; it
  runs through **unseeded pour copper** and **multi-net-seeded pours**.
- The only thing that moved coverage was **guessing** (nearest-seed / Voronoi),
  which mislabels boundary copper → false shorts/opens. Off the table.

The whole "points-only residual" turned out to be a **measurement error**: the
diagnostics ran `build_net_map` without registration, so the netlist sat ~7.8 mm
off the copper and its points landed in the wrong polygons — which mimics an
over-merge. Registered (the real pipeline), pcbtools is **97%**. There is no
points-only residual to fix, and no net-aware flood is warranted.

**Conclusion:** the residual is a computational-geometry problem — correctly
filling self-winding Gerber regions (true exterior + interior rings from one
keyhole path, or nonzero-winding coverage) — *not* a labelling problem. The
clear-polarity antipad case (separate LPC flashes) is already handled (§1). The
keyhole case is the remaining, harder work; it belongs here, not under a "flood".

### 3. Bridge precision (cross-layer)
Plated-through-hole bridges currently union *every* polygon the hole point lands
in on every layer. Restrict to the via's own net (KiCad gives it) and skip
polygons the point only lands in because of a missing antipad hole (fixed by #1).

### 4. Make coverage a tracked metric — DONE (#52)
`NetMap.coverage()` exists, and `tests/test_net_coverage.py` pins a floor
(pcbtools > 90% registered, < 10% un-registered). This is the guard that would
have caught the un-registered-measurement mistake above immediately. A check can
also gate on `coverage()` before upgrading itself from advisory to definitive.

### 5. Performance
The edge test is O(|edges_a|·|edges_b|) per candidate pair with a per-edge bbox
reject. It is fast enough today (droyd union ~6 s), but for dense/large boards
add a per-polygon **edge R-tree** so a pour's thousands of edges are queried, not
scanned. Needed before raising the corpus to bigger boards.

## Sequencing
#1 (holes) is the keystone: it is the "antipad/plane" deliverable *and* the
biggest connectivity lever, and it is safe (more-correct geometry, never a
mislabel). #2 unlocks the points-only netlist case but is the riskiest (label
propagation) and should land with coverage-metric guards from #4. #3 and #5 are
incremental hardening.
