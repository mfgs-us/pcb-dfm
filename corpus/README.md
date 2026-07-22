# Trust corpus

A growing set of **real boards** with **known-true expectations**, run against the
full ruleset on every CI build. Where the golden baselines (`tests/baselines/`)
pin exact output digests of *synthetic* boards to catch any change, the corpus
asserts *semantic invariants* on *real* boards to catch **false positives and
false negatives** — the failures that only show up on real artwork.

The corpus is deliberately seeded with a single board today
(`testdata/mini_board.zip`) and is designed to grow. **It is not yet
representative** — adding real boards (especially ones with known-good and
known-bad features, and design data) is the highest-value contribution here.

## Manifest format

One JSON file per board under `corpus/manifests/`:

```jsonc
{
  "name": "my_board",
  "board": "path/to/board.zip",      // relative to the repo root
  "ruleset": "default",              // optional, defaults to "default"
  "design_data": "path/to/proj/",    // optional: KiCad dir / IPC-2581 / sidecar
  "description": "...",
  "source": "where it came from / license",
  "expect": {
    "min_checks_run": 40,            // at least this many checks executed
    "no_crashes": true,              // no check crashed (surfaced as an error)
    "overall_status": "pass",        // optional; str or list of allowed values
    "status": {                      // per-check expected status
      "min_trace_width": "pass",
      "impedance_control": "not_applicable",
      "min_annular_ring": ["pass", "warning"]   // a set of allowed values
    },
    "must_not_fail": ["plating_uniformity"],     // false-positive guards
    "must_fail": ["min_trace_width"]             // known-bad features (true positives)
  }
}
```

Only the keys you specify are checked — assert **what you are confident about**,
not a full snapshot. Prefer invariants you can justify (a known-good feature must
not fail; a deliberately-bad feature must fail; SI checks are `not_applicable`
without design data) over pinning a whole verdict you have not ground-truthed.

## Running

```bash
pytest tests/test_corpus.py -v
```

## Adding a board

1. Drop the Gerber/Excellon `.zip` somewhere in the repo (or reuse `testdata/`).
2. Add a manifest under `corpus/manifests/` with the expectations you can vouch
   for. Note the board's source/license in `source`.
3. Run the harness; refine the expectations to what is genuinely true.
