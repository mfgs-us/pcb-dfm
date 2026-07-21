# Trust corpus & benchmarking

This engine is validated at three levels. The goal is to move from "the logic is
correct on toy inputs" toward "the numbers are trustworthy on real boards."

## 1. Property-based invariants (`tests/test_properties.py`)

Instead of one hand-picked input, these assert relationships that must hold for
**any** board, which catches whole classes of geometry bugs:

- **scale by k** → distance/size metrics scale by k (unit / scaling errors),
- **translate** → measured values unchanged (absolute-coordinate leaks),
- **mirror** → measured values and counts unchanged,
- **determinism** → identical result across runs (nondeterminism / ordering).

Boards are synthesized deterministically by `tests/boards.py` and transformed at
emit time — no fixtures to maintain.

## 2. Golden regression (`tests/test_golden.py`)

A small corpus of **archetype boards** (`tests/boards.py::ARCHETYPES`) — a clean
2-layer, a 4-layer with GND/PWR planes, and a known-bad thin-trace board — is
run through the full engine, reduced to a normalized digest (statuses,
severities, confidences, rounded measurements, stackup, warnings), and compared
against a committed baseline in `tests/baselines/`. Any change in what the
engine reports on a whole board fails the test.

Regenerate baselines after an **intended** behavior change:

```bash
PCBDFM_UPDATE_BASELINES=1 pytest tests/test_golden.py
```

Review the baseline diff in code review — it is the human-readable record of how
a change moved the engine's output.

## 3. Real-board corpus & external benchmark — TODO (help wanted)

This is the level that eventually retires "not battle-tested," and it is a
**manual, ongoing curation task**, not something the synthetic tests cover:

- **Real boards.** Add open-source-hardware Gerber sets (2-layer, 4-layer, HDI,
  and a deliberately-bad board) with documented provenance and licenses. Commit
  small archives under `testdata/` (the `.gitignore` keeps `testdata/*.zip`) or
  use Git LFS if they grow, then add golden baselines for them.
- **External comparison.** For credibility, compare results against a reference
  DFM tool (e.g. Integr8tor) or a fab's own DFM report on the same boards, and
  record per-check agreement/disagreement here as a tracked table. Treat
  disagreements as issues to triage, not test failures.

Neither of these can be automated in CI without licensed board files and access
to a reference tool, so they live here as a documented process. Contributions of
license-clean boards (and their reference reports) are the most valuable thing
you can add. Tracked in issue #9.
