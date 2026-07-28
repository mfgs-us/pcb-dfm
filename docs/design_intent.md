# Design intent — spec

The recurring blocker across the design-review effort is **intent**: many checks
can't fire trustworthily because the artwork doesn't say what the board is
*supposed* to do. This spec defines a machine-readable **design-intent** input
that supplies that missing layer — and, crucially, the discipline for using it
without breaking the no-false-positive contract.

Status: **spec only** (no code yet). It unblocks the parked/​deferred checks
(#62, #66, #67, #72, plus the requirement-conformance dimension) and is the
keystone of dimension 3 in `docs/design_reviewer.md`.

## Prime directive: infer first, declare the residue
Nobody fills out a giant sidecar. So intent is sourced in precedence order, and
we only *ask* for what we genuinely can't derive:

```
declared sidecar  >  schematic-derived  >  name/MPN-inferred  >  default
```

- **Inferred** intent (net function from names, part class from refdes, diff-pairs
  from KiCad, pin roles from a schematic) is **advisory-confidence** — a check
  built on it may warn, never hard-fail.
- **Declared** intent (from the sidecar) is **authoritative** — a check may
  promote to gating (e.g. IPC-2152 current can *fail* when the current is stated).
- **Absent** intent ⇒ the check is `not_applicable`. Never guessed. (The Tier-2
  contract in `design_intel`, extended to intent.)

## What's already inferred vs. what must be declared
The tool already derives a lot; the sidecar is only the part that isn't written
anywhere machine-readable.

| Already inferred (no sidecar) | Must be declared (the sidecar) |
|---|---|
| net function power/ground/signal (`classify_net`) | rail **voltage** and **rated current** |
| part class (`classify_component`) | connector **type** (edge / mezzanine / FFC) |
| diff-pairs, controlled-impedance specs (KiCad) | impedance **targets** (Ω + tolerance) where undeclared |
| pad↔net, pad geometry | **populate**/assembly variant (DNP truth) |
| pin roles/NC (from a schematic, when ingested) | **mechanical** envelope / height keep-outs |
| | length-match **groups** + tolerance; **sequencing** |
| | flex/**bend** regions; intentional-**open** nets |

## Sidecar shape
A `*.intent.json` (or YAML) alongside the board, or embedded in the KiCad project.
Every field is optional; a check reads only what it needs. Illustrative:

```jsonc
{
  "board": {
    "outline_mm": {"max_x": 100, "max_y": 40},   // mechanical envelope
    "max_height_mm": 10,                           // enclosure lid clearance
    "thickness_mm": 1.6,
    "stencil_thickness_mm": 0.12
  },
  "rails": {
    "+3V3": {"voltage_v": 3.3, "current_a": 1.2, "tolerance_pct": 5},
    "+5V":  {"voltage_v": 5.0, "current_a": 2.0}
  },
  "nets": {
    "USB_DP": {"class": "usb_hs", "impedance": {"diff_ohm": 90, "tol_pct": 10},
               "terminate": "none", "max_len_mm": 60},
    "CLK_50M": {"class": "clock", "terminate": "series_source"},
    "ANT_FEED": {"open_ended": true}                // suppress dangling-stub flags
  },
  "net_classes": {
    "can": {"impedance": {"diff_ohm": 120, "tol_pct": 10}}
  },
  "buses": {
    "SDRAM_DQ": {"members": ["DQ0","DQ1","...","DQ15"], "length_match_mm": 2.5}
  },
  "parts": {
    "J1":  {"connector": "usb_c_edge"},             // -> connector_edge_placement
    "J8":  {"connector": "mezzanine"},              // interior is fine
    "U5":  {"power_w": 2.5},                         // thermal budget
    "R30": {"populate": false}                       // assembly truth (DNP)
  },
  "regions": [
    {"kind": "bend", "polygon": [[10,0],[20,0],[20,40],[10,40]]}  // flex
  ],
  "sequencing": [{"before": "+1V2", "after": "+3V3"}]
}
```

This maps onto the existing `DesignData` model (extend, don't replace): `rails`→a
new `RailSpec`; `nets`/`net_classes`→extend `ControlledImpedanceSpec` + a net-intent
map; `parts`→fields on `Component` (connector_type, power_w, populate); `regions`
→`KeepoutRegion` (already has `kind`); `sequencing`→new.

## What each check gets (the payoff)
| Check (issue) | Intent field | Unlocks |
|---|---|---|
| `trace_current_capacity` (#4 parked) | `rails[*].current_a` | IPC-2152 width-vs-current, can **fail** |
| `series_termination_missing` (#62) | `nets[*].terminate`, `class` | which nets need a source R |
| `connector_edge_placement` (#66) | `parts[*].connector` | edge types flagged when interior; mezzanine ignored |
| `dnp_artwork_consistency` (#67) | `parts[*].populate` | DNP vs artwork mismatch |
| `dangling_trace_stub` (#72) | `nets[*].open_ended` | suppress intentional breakouts/stubs |
| impedance conformance | `nets[*].impedance` | target vs computed `impedance_control` |
| `flex_bend_rules` (#5 parked) | `regions[kind=bend]` | bend radius / no-via-in-bend |
| bus length-match (new) | `buses[*].length_match_mm` | DDR/parallel skew |
| mechanical-envelope (new) | `board.outline_mm/max_height_mm` | parts/copper outside the envelope |
| power sequencing (new) | `sequencing` | rail-order sanity |

## Inference layer (so the sidecar stays small)
A pre-fill pass populates a *candidate* sidecar from what's derivable, marked
`"source": "inferred"`:
- rail **voltage** from the net name (`+3V3`→3.3 V); current left blank.
- connector **type** from the footprint/MPN via a small parts table.
- net **class** from name patterns (already in `_trace_geom.is_hs_name`).
- diff-pairs and impedance specs already come from KiCad.

The user reviews and *confirms/overrides* — turning inferred into declared only
where it matters. Checks treat `inferred` as advisory and `declared` as
authoritative, per the prime directive.

## Trust contract (why this doesn't reintroduce false positives)
1. **Absent ⇒ not_applicable.** No intent, no finding — never a guess.
2. **Inferred ⇒ advisory; declared ⇒ may gate.** Confidence rides with the source.
3. **Intent can *suppress*.** `open_ended`, `populate:false`, and acknowledgements
   silence findings the reviewer has judged intentional (the design-review
   suppression requirement).
4. **Schematic is the functional-intent source** (pin roles, NC, rails, blocks);
   the sidecar is only the *requirement* residue (currents, impedances, thermal,
   mechanical, sequencing). Ingesting the schematic shrinks the sidecar and
   sharpens the existing checks — build that first (it retroactively upgrades the
   16 shipped review checks and kills known FPs like the `BAT_NEG` ground case).

## Rollout
1. **Minimal sidecar** — `rails` + `nets.class/terminate/open_ended` + `parts.connector/populate`. Unblocks #62/#66/#67/#72 and IPC-2152 current at once.
2. **Inference pre-fill** — candidate sidecar from names/MPNs; user confirms.
3. **Schematic ingest** — functional intent (pin types, power symbols, hierarchy); also enables schematic↔layout consistency.
4. **Requirement-conformance checks** — current budget, impedance targets, mechanical envelope, sequencing, bus length-match.
