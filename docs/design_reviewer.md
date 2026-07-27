# From DFM engine to design reviewer

## The reframe
- **DFM** (what the engine is today): *"Can the fab and assembly house build this
  without yield loss?"* Anchored in physics and process — objective, geometry-
  driven, low false-positive by nature.
- **Design review** (the vision): *"Is this correct, robust, and what you actually
  intended?"* Anchored in **intent and best practice** — what a senior EE does in a
  peer review. A different, harder question, because intent isn't in the Gerbers.

The `design_advisory` tier (16 checks — refdes coverage, fiducials, teardrops,
floating copper, courtyard overlap, decoupling proximity) is already the seed of a
design reviewer. This doc is the plan to grow it into the whole layer.

## The prime directive carries over
The engine's entire value is that it does not cry wolf. A DFM false positive is
rare and bounded; a *review* false positive ("you're missing a pull-up") fires
constantly and erodes trust fast. So the reviewer layer must be **even more
conservative** than DFM:
- **advisory-only** — never hard-fails (reuse the `advisory()` helper / info tier);
- **evidence-cited** — every finding names the measured fact it rests on;
- **not_applicable, not guessed** — no intent input ⇒ `not_applicable`, never a
  fabricated opinion (the Tier-2 contract in `design_intel`);
- **suppressible** — a design choice the reviewer flagged wrongly must be silenceable.

No folklore. If a rule can't be stated objectively and tied to evidence, it doesn't ship.

## Five dimensions of review

### 1. Electrical-correctness review  *(buildable now — start here)*
Objective checks over the netlist + BOM the engine already ingests, using
`design_intel` (`classify_net`, `classify_component`, `is_decoupling_candidate`,
`PadNetIndex`). No new input needed.
- **Decoupling adequacy per rail** — caps per IC supply pin on each power net
  (first check; spec below).
- **Floating / single-pin nets** — a net with one connection is almost always a
  mistake or a stub.
- **Missing pull-up/-down** — nets that structurally need a bias (reset, I²C
  SDA/SCL, enable/boot) with no resistor to a rail. (Conservative: only well-known
  net-name patterns.)
- **Bulk capacitance per rail** — at least one bulk cap per supply.
- **Test-point coverage on critical nets** — extends `test_point_coverage` to
  *which* nets, not just how many.
These *feel* like a reviewer looked at the board and are all grounded in data we parse.

### 2. Component / datasheet best-practice  *(needs a per-part rule source)*
Crystal proximity + guard ring, switching-regulator loop area, feedback trace not
under the inductor, connector orientation. Intent- and part-specific; needs a
**rules pack** keyed by part class / MPN.

### 3. Intent / requirements conformance  *(needs a new input — the big lift)*
Does it meet its spec: impedance targets (have `impedance_control`), current budget
(the parked IPC-2152), mechanical envelope, layer count, connector pinout. Requires
a machine-readable **design-intent sidecar** — the single biggest architectural lift.
Moves the tool up-stack from copper to schematic-level intent.

### 4. Change / rev diff  *(needs a baseline rev)*
Compare rev N to N-1: nets/parts added or removed, DRC delta, "this change looks
unintentional." Reviewers do this constantly.

### 5. Presentation as a review, not a linter
Group findings by **subsystem / net / component** (not check category), rank by
impact, explain *why* with a citation, tier them must-fix / should-fix / consider.

## Architecture: engine as sensor, judgment on top
Keep the deterministic engine as the **trustworthy sensor** and add **judgment on
top** — don't blur them. The engine produces a rich, structured *design fact sheet*
(every net with role/members/geometry; every component with placement + BOM
identity; every clearance/impedance/thermal measurement). A reasoning layer — a
curated rules pack, or an LLM grounded strictly on those facts — does the intent-
level review, never inventing geometry, always citing back to a measured fact.
That split is how you get a reviewer's judgment without losing the trust that is
the whole moat.

## Roadmap
1. **Electrical-correctness checks** (dim. 1) — objective, high-trust, buildable on
   today's data. **Decoupling adequacy first.** Then floating/single-pin nets,
   missing bias, bulk capacitance, critical-net test points.
2. **Design-intent sidecar** (dim. 3) — declare rail voltages/currents, critical
   nets, mechanical envelope, net roles. Unlocks conformance checks and de-risks the
   heuristics in dim. 1.
3. **Rules pack** (dim. 2) — per-part best-practice, opt-in per MPN/class.
4. **Grounded reasoning layer** (frontier) — LLM review over the fact sheet, cited.
5. **Review-shaped reporting** (dim. 5) — throughout.

---

## First check spec — `decoupling_adequacy`
**Dimension 1. Category `design_advisory` (info tier, never hard-fails).**

**Why:** an IC power rail with no local bypass is a classic review catch — noise,
brown-out, marginal behaviour. It's also a strong data-completeness signal.

**Inputs (else `not_applicable`):** components with pads, a netlist (net access
points), and BOM/ref identity — i.e. enough for `PadNetIndex` + `classify_*`.

**Method:**
1. Build `PadNetIndex`; classify nets (`classify_net`) and parts (`classify_component`).
2. For each **power** net that feeds ≥1 IC pad (a real supply rail):
   - `supply_pins` = IC pads on that net.
   - `decouple_caps` = decoupling-candidate capacitors (`is_decoupling_candidate`)
     with a pad on that net (and, when resolvable, the other pad on a ground net).
   - `ratio = decouple_caps / supply_pins`.
3. **Flag (warning):** a supply rail with `supply_pins ≥ 1` and **zero** decoupling
   caps — high-confidence omission. **Advisory (warning):** `ratio < 0.5` — light
   decoupling; "consider more local bypass."
4. **Metric:** worst rail's caps-per-supply-pin ratio. Report the offending rails.

**False-positive guards:** rails with no IC pins are skipped (connector/passive
rails aren't a decoupling concern); unresolved pads never manufacture a supply pin;
no netlist/BOM ⇒ `not_applicable`. Conservative thresholds; advisory only.
