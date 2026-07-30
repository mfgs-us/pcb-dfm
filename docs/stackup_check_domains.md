# Stackup check gaps — spec

Twelve gaps in the stackup domain, found by auditing the shipped stackup-aware
checks (`stackup_symmetry`, `impedance_control`, `dielectric_thickness_uniformity`,
`layer_registration_margin`, `signal_plane_adjacency`, `microvia_geometry`,
`drill_aspect_ratio`, `copper_balance_plating`) against what a fabricator
actually queries at CAM.

Two parts:

- **§1–§7 — the stackup's *numbers*.** Thicknesses, copper weights, aspect
  ratios, impedance geometry. Five new checks; two (§1, §7) fix a shipped check
  that is measurably wrong or blind rather than adding a new id.
- **§8–§12 — the stackup's *structure*.** Layer ordering, construction validity,
  lamination sequence. Nothing in the catalogue looks at the shape of the stack
  itself today, only at the values inside it. §12 is an ingest prerequisite, not
  a check.

Ordered by value/effort within each part.

**Status (2026-07):** all twelve spec'd and filed (#92–#98, #99–#103).
**§8–§12 are built** (structural group + the ODB++ ingest fix, including the
`stackup_symmetry` material comparison §10 unblocked). §1–§7 remain open.

Conventions (same as `new_check_domains.md`): every check is `not_applicable`
without its inputs, states a metric with target/limit, and follows the tier rules
(design-advisory never hard-fails; fab may). No folklore — objective, sourced
rules only. Each must be validated against real geometry before it ships, with
false positives on the trust corpus treated as a blocker, not a footnote.

---

## 1. Etch floor must scale with copper weight — `etch_compensation_margin` fix  [#92]

**Why:** minimum etchable line and space is a function of foil thickness — a
thicker foil needs a longer etch, and the longer etch undercuts the resist, so
the conductor comes out trapezoidal and the *gap* comes out wider than drawn.
`impl_etch_compensation_margin.py:28-35` hardcodes the floor at
**0.075 mm** ("typical of standard 1 oz outer copper"), overridable only via
`raw.etch_capability_mm`. It never reads the stackup. So a board drawn with
0.1 mm traces and 2 oz outer copper passes `min_trace_width`,
`min_trace_spacing` **and** `etch_compensation_margin` today, and no mainstream
fab will hold it.

This is the highest-value item in this document because the failure is invisible
to all three existing checks by construction: their thresholds are fixed
constants, and the input that makes the geometry infeasible (copper weight)
is in a different file.

**Inputs:** copper geometry (already collected) + `StackupLayer.thickness_mm`
for the copper layer the feature is on. Note the model documents
`copper_thickness_mm` as *finished* copper thickness, so the table below is
keyed on finished thickness, not base foil.

**Rule:** replace the constant with a table lookup, linearly interpolated, on
the finished copper thickness of the layer under test:

| Finished copper | Nominal | Min line / space |
|---|---|---|
| 17 µm | 0.5 oz | 0.075 mm |
| 35 µm | 1 oz | 0.100 mm |
| 70 µm | 2 oz | 0.150 mm |
| 105 µm | 3 oz | 0.200 mm |
| 140 µm | 4 oz | 0.250 mm |

Above 140 µm, extrapolate at the 3→4 oz slope rather than clamping — heavy
copper gets worse, not flat. `raw.etch_capability_mm` stays authoritative when
set (an explicit fab capability beats our table). With no stackup, keep today's
0.075 mm default **and say so in the message** — the current behaviour is the
no-data fallback, not the rule.

**Metric:** unchanged (percent margin to the floor). **Category:**
`copper_geometry`.

**Guards / false positives:** per-layer, not board-wide — outer layers are
commonly 1 oz base plated up to ~2 oz finished while inners stay 0.5 oz, so
applying one weight to every layer would fabricate failures on the inners. When
a copper layer carries no `thickness_mm`, fall back for that layer only.

---

## 2. Blind and buried via aspect ratio by actual span — `blind_buried_via_aspect_ratio`  [#93]

**Why:** `impl_drill_aspect_ratio.py:46-49` documents using full board thickness
for every hole as an "accepted limitation … conservative". It is not
conservative — it is wrong in both directions at once:

- the **depth** is over-estimated (a blind via spanning L1–L2 gets the whole
  board thickness as its numerator), and
- the **limit** is under-applied (a mechanically drilled blind via has a *closed
  bottom*, so plating solution cannot exchange freely and the practical ceiling
  is around **1:1**, versus 8–10:1 for a through hole).

Net effect on a real HDI board: the check reports a scary ratio against the
wrong denominator while never applying the tighter rule that actually governs
the hole. A blind via at 0.3 mm drill through 0.4 mm of stack (1.33:1 — not
manufacturable) is indistinguishable today from one at 0.2 mm depth (0.67:1 —
fine), because both are reported as `board_thickness / drill`.

**Inputs:** `Via.via_type` in `("blind", "buried")` + `Via.drill_mm` +
`from_layer`/`to_layer` + an ordered stackup. The KiCad adapter already parses
via type and drill (`(via micro …)`, shipped with §6 of `new_check_domains.md`);
`_span_depth(stackup, a, b)` already exists at `impl_microvia_geometry.py:44`
and should be lifted to a shared helper rather than copied.

**Rules:**
- **Blind** (outer to inner, mechanically drilled, closed bottom): depth =
  span dielectric + copper between the two layers. Target **≤ 0.8:1**, limit
  **≤ 1.0:1**.
- **Buried** (inner to inner): drilled through its sub-core *before* lamination,
  so it is a through hole of that core and the standard plating limit applies to
  the core thickness — target **≤ 8:1**, limit **≤ 10:1** (reuse the
  `drill_aspect_ratio` thresholds; do not invent a second set).
- Microvias stay with `microvia_geometry` (0.75:1 / 1.0:1) — this check must
  skip `via_type == "micro"` so the two never double-report.

**Metric:** worst aspect ratio (dimensionless, `:1`). **Category:**
`drill_via_integrity`. **Severity:** `error` (matches `microvia_geometry`).

**Also fix:** `drill_aspect_ratio` should state in its message that blind/buried
holes are excluded and covered here, and drop the stale "accepted limitation"
paragraph. Without that, the two checks contradict each other in the report.

---

## 3. Minimum dielectric thickness floor — `min_dielectric_thickness`  [#94]

**Why:** `dielectric_thickness_uniformity` measures deviation from the **mean**
only (`impl_dielectric_thickness_uniformity.py:55-57`), so a stackup of
uniformly 0.04 mm dielectrics scores a clean pass — perfectly uniform, and
perfectly unbuildable. Nothing in the catalogue enforces an absolute floor.

The floor is a real purchasing constraint, not a preference: the thinnest
standard prepreg styles are 106 (≈0.05 mm) and 1080 (≈0.076 mm), and below a
single pressed sheet you risk resin starvation and layer-to-layer shorts.

**Inputs:** `stackup.dielectric_thicknesses_mm()` (≥ 1 usable value).

**Rule:** report the **thinnest** dielectric in the stack. Warn below
**0.075 mm** (below one 1080 sheet — buildable only with a thinner specialty
style, so it is a re-quote), fail below **0.05 mm** (below any standard sheet).
Report which layer, not just the value.

**Metric:** minimum dielectric thickness (µm, `preferred_direction: maximize`,
`limits.min`). **Category:** `fabrication_stackup`. **Severity:** `warning`.

**Explicitly out of scope:** IPC-2221 voltage-dependent internal-layer spacing
would tighten this floor substantially on a high-voltage design, but no input
format we read carries per-net voltage, so this check states the
fab-capability floor only. Do not imply otherwise in the message. (If a
voltage-annotated sidecar ever lands, this is where it plugs in — same pause
rationale as §4 IPC-2152 current in `new_check_domains.md`.)

---

## 4. Stackup vs artwork copper-layer-count consistency — `stackup_artwork_consistency`  [#95]

**Why:** nothing cross-checks the two sides, though both sit in the same
`CheckContext`: `stackup.copper_layers()` and
`queries.get_copper_layers(ctx.geometry)`. A stackup declaring 6 copper layers
against 4 exported copper Gerbers is one of the most common CAM queries there
is — the fab quotes and tools a 6-layer build, then finds 4 films, and the job
stops. The reverse (artwork richer than the declared stack) means the fab
drawing is stale.

Cheap to build, and it doubles as adapter validation — a silent stackup-parse
regression currently shows up as checks quietly going `not_applicable`, which
is indistinguishable from "no data supplied".

**Inputs:** an ordered stackup **and** detected copper artwork layers.

**Rule:** compare counts. Any mismatch is a finding; report both numbers and
the layer names on each side so the user can see which is missing.

**Guards / false positives** — this is the whole risk of the check, since our
copper-layer detection is filename-token based:
- Require **≥ 2 copper layers in the stackup**, the same guard
  `stackup_symmetry` uses (`impl_stackup_symmetry.py:59-72`) to reject the
  flat scalar sidecar synthesis, which produces exactly one copper layer and
  would otherwise report a spurious mismatch against every board.
- `not_applicable` when either side is indeterminate — never guess.
- Validate against every corpus board before shipping. If filename-token
  detection produces even one false mismatch on the trust corpus, cap the
  finding at `warning` instead of `error`.

**Metric:** absolute layer-count difference (dimensionless, target 0).
**Category:** `fab_process_compatibility`.

---

## 5. Stacked / staggered microvia limits (HDI) — `stacked_microvia_limit`  [#96]

**Why:** `microvia_geometry` grades each microvia in isolation — depth/diameter
and single-dielectric span. It says nothing about how microvias **relate** to
each other, which is where HDI builds actually fail. Stacking microvias
directly on top of each other concentrates plating stress at the target pad and
is capability-limited: 2-high is mainstream, 3-high is specialty, and a
microvia landing on an *unfilled* through via has nothing flat to plate onto.

**Inputs:** `Via` list with `via_type == "micro"`, `x_mm`/`y_mm`,
`from_layer`/`to_layer`, and an ordered stackup for span adjacency.

**Rules:**
- Group microvias whose centres coincide within a tolerance (default 0.05 mm)
  **and** whose spans are consecutive in the stack → that is a stack. Stack
  height target **≤ 2**, limit **≤ 3**.
- A microvia coincident with a **through** via at the same location →
  finding: the through via must be filled and planarized first. We cannot
  confirm fill from any input we read, so this caps at **warning** and the
  message must say the assumption out loud.
- Microvias on consecutive spans that are offset but by **less than one pad
  diameter** are neither stacked nor properly staggered → informational.

**Metric:** maximum stack height (count, target ≤ 2, limit ≤ 3). **Category:**
`drill_via_integrity`. **Severity:** `warning` (a 3-high stack is a real build,
just not a cheap one).

**Effort note:** the only item here needing real spatial grouping. Validate on
the KiCad QA HDI boards already used for `microvia_geometry` (issue22536,
issue18142) — those are GPL and not vendored, so committed tests reproduce the
geometry synthetically, same as §6 of `new_check_domains.md`.

---

## 6. Declared vs constructed board thickness — `board_thickness_consistency`  [#97]

**Why:** if the designer declares 1.6 mm and the layer stack sums to 1.43 mm,
one of the two is wrong, and both feed real decisions — connector fit, press-fit
hardware, edge-connector thickness, and the `drill_aspect_ratio` denominator.
Nothing catches the disagreement because **no declared thickness exists in the
model at all**: `grep board_thickness` finds only `drill_aspect_ratio`'s local
helper, and the KiCad adapter parses the stackup block but not
`(general (thickness …))`.

**Blocked on a small ingest change** (hence lower priority than §1–§5, not
lower value):
1. New optional `DesignData.board_thickness_mm`.
2. KiCad adapter: parse `(general (thickness …))`.
3. IPC-2581 adapter: parse total thickness where present.

**Rules:**
- Sum the stack (`total_thickness_mm()`), compare to declared. Warn beyond
  **±10 %**, fail beyond **±20 %**.
- Report the finished thickness against standard offerings (0.4, 0.6, 0.8, 1.0,
  1.2, 1.6, 2.0, 2.4, 3.2 mm) as **informational only** — a custom thickness is
  legitimate, it just means a re-quote and a longer lead time. Never fail on it.

**Critical detail — the two numbers are not the same quantity.** The KiCad
adapter deliberately **skips** solder mask and silkscreen when building the
stack (`kicad.py:188-190`: "not part of the electrical stack the checks reason
about"), while KiCad's own `(general (thickness))` is the full board thickness
*including* mask. So `total_thickness_mm()` is systematically ~20–70 µm light
against the declared value on every board. Either compare copper+dielectric
consistently on both sides, or absorb the offset in the tolerance — but do not
ship a check that warns on every KiCad board because of a definitional
mismatch. This is the acceptance criterion that matters here.

**Metric:** signed deviation from declared thickness (%). **Category:**
`fabrication_stackup`. **Severity:** `warning`.

---

## 7. Per-layer impedance geometry — `impedance_control` fix  [#98]

**Why:** `impedance_control` resolves its geometry from **representative**
stackup values rather than the geometry the net actually sits in
(`impl_impedance_control.py:96-107`):

- `er` = the *first* dielectric in the stack that carries an Er
  (`design_model.py:47-53`),
- `h_mm` = the *first* dielectric that carries a thickness,
- `b_mm` = `sum(dielectric_thicknesses_mm())` — **the entire stack** — as the
  stripline plane-to-plane separation.

On a 2-layer board that is right. On a 6-layer board an inner stripline gets the
whole board's dielectric as `b`, which inflates Z₀ substantially, and a mixed-Er
build (Rogers outer / FR-4 inner, or any hybrid) silently applies the wrong Er
to every net. The check reports specific ohms with the authority of a
calculation, and the number can be far off.

This is worth more than some of the new checks above: it is the difference
between a 🔬 check being trustworthy and being decorative. Both formulas
themselves are fine (IPC-2141) — the inputs are what's wrong.

**Rules:**
- Resolve the controlled net's **layer** to its index in `stackup.layers`, then
  walk outward to find its actual reference plane(s).
- **Microstrip** (outer layer): `h` = the single adjacent dielectric; `er` =
  that dielectric's Er.
- **Stripline** (inner layer): `b` = separation between the two *bracketing
  reference planes* only; `er` = thickness-weighted mean of the dielectrics
  between them.
- `t` = that copper layer's own `thickness_mm`, not the first copper layer's.
- Identify planes with the coverage heuristic `signal_plane_adjacency` already
  uses (`_coverage` / `plane_coverage`, default 0.65 — `impl_signal_plane_adjacency.py:35,54`),
  and lift it to a shared helper. Do not add a second, differently-tuned plane
  detector.
- An explicit `spec.height_mm` stays authoritative over all of this — it is the
  user telling us the answer.
- Keep today's representative-value path as the documented fallback for when the
  net's layer can't be located in the stack, and say in the message which path
  was used. A number whose provenance is unstated is the actual bug here.

**Metric:** unchanged (deviation from target impedance, %). **Category:**
`fabrication_stackup`.

**Acceptance:** a 6-layer fixture where the representative-value path and the
resolved path disagree by a wide margin, asserting the resolved path is used and
lands within tolerance. Regenerate golden baselines — this **will** move
existing `impedance_control` numbers on multilayer corpus boards, and that
movement is the point, so eyeball each diff rather than blessing it wholesale.

---

# Part 2 — structural checks (§8–§12)

§1–§7 all reason about the stackup's *numbers*. Nothing in the catalogue
validates its *shape*: whether the layer list is a physically possible build,
whether it is ordered the way its own names claim, or what lamination sequence it
implies. These are cheap (pure list analysis, no artwork) and they protect the
checks already shipped — §8 in particular is the precondition the rest of the
domain silently assumes.

---

## 8. Stackup construction validity — `stackup_construction_validity`  [#99] [DONE]

**Why:** two shipped checks assume the stack is well-formed and neither verifies
it. `stackup_symmetry` pairs layer *k*-from-top with *k*-from-bottom
(`impl_stackup_symmetry.py:87-109`) and `_span_depth` at
`impl_microvia_geometry.py:44` slices `layers[i+1:j]` — both assume strict
copper/dielectric alternation in top→bottom order. Violate that and they return
a confident number computed from nonsense, with no signal that anything is wrong.

There is also a live path that can produce a malformed stack from valid input:
`ipc2581.py:100-102` falls back to "an Er implies dielectric, otherwise copper"
when a `<StackupLayer>` carries no layer function. On a stackup with no functions
declared, that yields an **all-copper stack** which no current check questions.

**Inputs:** an ordered stackup (≥ 1 layer). No artwork.

**Rules:**
- **copper–copper adjacency → fail.** Two copper layers with no dielectric
  between them is not a build; it is a parse error or a malformed sidecar.
- **adjacent dielectrics → not a finding, ever.** Multiple prepreg sheets
  between cores are normal construction. This is *the* false positive to design
  against; put it in the tests.
- **first and last layer must be copper** → fail otherwise: a stack that starts
  or ends on dielectric is missing its outer foil.
- **duplicate layer names → fail** (merge or parse error).
- **data sanity:** non-positive thickness on any layer; `er <= 1.0`
  (physically impossible); `er` outside ~2.0–10.0 → units error, report as a
  data fault rather than a DFM violation.
- **odd copper-layer count → informational.** Legal, but rare, forces asymmetry,
  and gets re-quoted. Never a failure.

**Metric:** count of structural faults (dimensionless, target 0). **Category:**
`fabrication_stackup`. **Severity:** `error` — unlike most of this document
these are not process-margin judgments, they are "this cannot be built as
described".

**Guards:** apply the same ≥ 2 copper-layer guard `stackup_symmetry` uses
(`impl_stackup_symmetry.py:59-72`) so the flat scalar-sidecar synthesis — one
copper layer followed by every dielectric — is `not_applicable` here rather than
reported as a stack that starts on copper and ends on dielectric. Without that
guard this check fails on every scalar sidecar.

---

## 9. Layer ordering vs layer naming — `stackup_layer_order`  [#100] [DONE]

**Why:** a stack whose order disagrees with its own layer names is a mirrored or
transposed build, and **every geometric check in the catalogue passes it
cleanly** — the board comes back electrically wrong, not geometrically wrong.
That makes it invisible to everything we currently run.

Ordering trust also varies by adapter today, and nothing reconciles them:

| Adapter | Order source |
|---|---|
| ODB++ | sorts by matrix `ROW` (`odbpp.py:141`) — explicit |
| KiCad | file order of `(layer …)` in the stackup block |
| IPC-2581 | raw document order — **no `sequence` attribute is read** (`grep -n sequence pcb_dfm/ingest/adapters/ipc2581.py` is empty) |

The one format that carries an explicit stackup sequence is the one where we
trust document order blindly.

**Inputs:** an ordered stackup whose copper layers carry parseable names.

**Rules:**
- Parse an inner-layer index from each copper layer name (`In1.Cu`, `L2`,
  `SIG2`, …). Indices must **increase monotonically** top→bottom.
- The first copper layer must carry a top token (`F.Cu`, `TOP`, `L1`), the last
  a bottom token (`B.Cu`, `BOT`).
- No gaps in inner numbering — `In1, In2, In4` means a layer is missing from the
  stack (distinct from §4, which counts artwork films; this reads the names).
- Name-derived order disagreeing with list order → the stack is transposed or
  mirrored relative to its own naming.

**Metric:** count of ordering faults (dimensionless, target 0). **Category:**
`fabrication_stackup`. **Severity:** `error`.

**Guards / false positives:** naming conventions vary widely, so assert
monotonicity **only** when ≥ 2 inner copper names parse to indices; unparseable
names → `not_applicable`, never a guess. `_layer_order_key`
(`impl_signal_plane_adjacency.py:25`) has the regex idiom to borrow, but it
operates on *geometry* layers (`side` / `logical_layer`), not stackup layer
names — this needs its own parser.

**Also in scope:** read the IPC-2581 `<StackupLayer>` sequence attribute in the
adapter and order by it. Validating order while ignoring the field that declares
it would be backwards.

---

## 10. Core / prepreg lamination validity — `stackup_lamination_validity`  [#101] [DONE]

**Why:** this is where the real lamination rules live, and we currently throw
away the input they need. **All three adapters collapse core and prepreg into
`kind="dielectric"`** — `kicad.py:185-186`, `_DIELECTRIC_FUNCS` at
`ipc2581.py:41`, `_DIELECTRIC_TYPES` at `odbpp.py:64` — and `StackupLayer`
(`design_model.py:24-29`) has no field to hold the distinction.

**This is already costing us a shipped check.** `stackup_symmetry` compares only
`kind`, which is `copper` or `dielectric` (`impl_stackup_symmetry.py:91`), so a
**core mirrored by prepreg passes today** whenever the two thicknesses happen to
match — and that is precisely the construction asymmetry that warps on reflow.
The check cannot see it because the information is discarded three layers
upstream. Fixing the model fixes `stackup_symmetry` as a side effect, which is
the strongest argument for doing it.

**Model + adapter change (DONE):**
1. Add a `material` / `subkind` field to `StackupLayer` (`core` | `prepreg` |
   `dielectric` when unknown).
2. KiCad: preserve the existing `core`/`prepreg` type token instead of
   collapsing it.
3. IPC-2581: keep the specific `_DIELECTRIC_FUNCS` member (`CORE`, `PREPREG`,
   `DIELPREPREG`) rather than mapping them all to one kind.
4. ODB++: same for `_DIELECTRIC_TYPES`.
5. Extend `stackup_symmetry`'s mirror comparison to compare material, not just
   kind — a separate, small PR, and it will move baselines.

**Rules (implemented):**
- **At least one core** in the build → fail otherwise. An all-prepreg stack has
  nothing rigid to register to.
- **No core–core adjacency** → fail. Cores do not bond to each other; prepreg is
  the adhesive.
- **Prepreg between cores ≥ ~0.1 mm** (or ≥ 2 sheets) for a reliable bond →
  warn below.
- Material asymmetry across the mid-plane → belongs in `stackup_symmetry`, not
  here. Do not report it twice.

**Metric:** count of lamination faults (dimensionless, target 0). **Category:**
`fabrication_stackup`. **Severity:** `warning` for the bond-thickness rule,
`error` for the structural ones.

**Guards:** `material` will be unknown on most real input for a while. Every
rule must degrade to `not_applicable` per-rule when the material is unknown, not
report a fault. A stack with no material data at all is `not_applicable`.

---

## 11. Implied lamination cycles — `lamination_cycle_count`  [#102] [DONE]

**Why:** the number of lamination cycles a build implies is what a quote is
actually priced on, and it is derivable: nested and overlapping blind/buried via
spans force sequential lamination. Reporting "this is a 2+N+2 build requiring 3
press cycles" tells a designer something no other check does, and it is the
natural home for the foil-vs-cap-lamination build classification too.

**Inputs:** an ordered stackup + `Via` spans (`via_type`, `from_layer`,
`to_layer`). Not purely stackup-intrinsic, unlike §8–§10.

**Rules:**
- Derive the build class from the via spans: microvia spans on the outer
  dielectrics give the `N+N` prefix/suffix (`1+N+1`, `2+N+2`), buried spans give
  the sub-lamination core.
- Report the implied press-cycle count and the build class.
- **Informational only — never pass/fail.** The derivation is objective; "too
  many cycles" is a cost judgment, not a manufacturability rule. This is the
  discipline line: we report the build, the designer prices it.

**Metric:** implied lamination cycle count (dimensionless, informational).
**Category:** `fabrication_stackup`. **Severity:** `info`.

**Note:** if this ever wants a threshold, it needs a sourced fab capability
limit, not a guess about what is expensive. Absent that, it stays informational.

---

## 12. ODB++ carries no layer thicknesses — ingest gap  [#103] [DONE]

**Not a check.** `_parse_matrix` builds `StackupLayer(name=name, kind=kind)` with
**no `thickness_mm` and no `er`** (`odbpp.py:139`). Consequence: every
thickness-based stackup check — `stackup_symmetry`,
`dielectric_thickness_uniformity`, §3 `min_dielectric_thickness`,
`impedance_control`, `microvia_geometry`'s depth, `drill_aspect_ratio`'s
denominator — silently reports `not_applicable` on ODB++ input, which is
indistinguishable from "the user supplied no design data".

ODB++ jobs generally do carry thickness and material data, just not in the
matrix `LAYER` records that `_parse_matrix` reads. Locate where (job-level
stackup/info files, not the matrix) and populate the fields.

Until then, §8 and §9 are the **only** stackup checks that function on an ODB++
job — which is a reason to build them early, and a reason this gap is worth
closing.
