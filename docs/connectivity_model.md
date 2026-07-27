# Net connectivity model — status and completion plan

The net-aware checks (`via_to_copper_clearance`, `min_trace_spacing`,
`solder_mask_web`, `copper_sliver_width`, `diff_pair_spacing`,
`return_path_interruptions`, `crosstalk_estimate`, `tombstoning_risk`) all rest
on one question: **is this copper the via/trace's own net, or a foreign net?**
That is answered by `geometry/net_map.py`, which labels each copper polygon with
a net. The more copper it labels, the more of those checks can be *definitive*
(hard pass/fail) instead of *advisory*.

## Where it stands

Coverage measured as `tagged_polygon_count / total copper polygons`:

| Board | Design source | Seeding | Coverage |
|---|---|---|---|
| droyd | KiCad | routed segments + points | **75%** |
| pcbtools | IPC-D-356 | access points only | **3%** |

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
- Extend `Polygon` with `holes: List[List[Point2D]]` (interior rings), default
  empty so every existing call site is unchanged.
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

### 2. Net-aware union for the points-only residual
Even with holes, IPC-D-356 seeds are sparse. Add a *label-respecting* union:
never merge two polygons already carrying **different** net seeds (that is always
a false merge or a real short — neither should silently collapse the group).
Then flood labels outward from seeds and **stop at a conflict boundary** rather
than discarding the whole group. Care required: an unseeded polygon reachable
from two nets must stay unlabelled (order-independence), so implement as
"label P as net N iff every seed reachable from P through same-conductor copper
is N", computed per connected component after the label-respecting union.

### 3. Bridge precision (cross-layer)
Plated-through-hole bridges currently union *every* polygon the hole point lands
in on every layer. Restrict to the via's own net (KiCad gives it) and skip
polygons the point only lands in because of a missing antipad hole (fixed by #1).

### 4. Make coverage a tracked metric
Add `NetMap.coverage()` and assert a per-board floor in the golden corpus so a
regression in labelling is caught, and so the checks that *upgrade* to definitive
(once coverage is high enough) can gate on it explicitly.

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
