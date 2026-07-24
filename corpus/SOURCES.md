# Corpus board provenance

Every real board in `testdata/` is third-party artwork. This file records where
each came from and under what licence, so the corpus stays redistributable.

This project is Apache-2.0. Only **attribution-only** licences are accepted here
(MIT / BSD / Apache / CC-BY). Boards under reciprocal or share-alike terms are
deliberately *not* vendored, even where they would be useful.

| board | design | licence | via |
|---|---|---|---|
| `pcbtools_example.zip` | pcb-tools example board | Apache-2.0 | [curtacircuitos/pcb-tools](https://github.com/curtacircuitos/pcb-tools) `examples/gerbers` |
| `pcbtools_full.zip` | same design, complete 8-file export | Apache-2.0 | [curtacircuitos/pcb-tools](https://github.com/curtacircuitos/pcb-tools) `gerber/tests/resources` |
| `eagle_gyw.zip` | GYW Electro Curriculum board (Autodesk Eagle) | MIT, © 2019 Ganz Youth Workshop | [GanzYouthWorkshop/GYW-Electro-Curriculum](https://github.com/GanzYouthWorkshop/GYW-Electro-Curriculum), via gerbonara `tests/resources/eagle-newer` |
| `diptrace_fd1.zip` | FD1 project mainboard (DipTrace) | BSD 3-clause, © 2014 Przemysław Węgrzyn | [codepainters/FD1](https://github.com/codepainters/FD1), via gerbonara `tests/resources/diptrace` |
| `mini_board.zip` | synthetic | this project | — |

### Netlists

| file | for | licence | via |
|---|---|---|---|
| `pcbtools_full.ipc` | `pcbtools_full.zip` | Apache-2.0 | [curtacircuitos/pcb-tools](https://github.com/curtacircuitos/pcb-tools) `gerber/tests/resources/ipc-d-356.ipc` |

An IPC-D-356 netlist for the same design as `pcbtools_full.zip`, from the same
source. Verified to be that board: all 30 of its through-hole records register
onto real drill hits once the netlist's origin offset (the board's lower-left
corner) is derived. It is what makes the net-aware path testable on real
artwork rather than only on synthetic fixtures.

It doubles as **placement data**: every 317/327 record names the component and
pin it belongs to, so 21 components (C1-C5, DMX, J1, L1, LED1, MIDI, PWR, R1-R5,
U1-U4) with per-pin locations are derived from it. That is what makes the
footprint-aware checks testable on real artwork too. One quirk to be aware of:
the file carries a single record with the placeholder refdes `NA` on net
`NNAME1`, which is test scaffolding for long net names and duplicates the
location of `U4-8`.

Gerber files are byte-identical to upstream. Some were **renamed** to
conventional extensions (e.g. `copper_top.gbr` → `board.gtl`) so that layer
classification happens by extension; no content is modified.

## Regression baselines

Each board here has a committed golden digest under `tests/baselines/corpus/`,
covering all 46 checks' status and measured value, produced with the board's own
design data where it ships some. The corpus manifests assert that specific
checks must not fail; the goldens catch quiet drift in the *numbers* — a value
moving because a shared helper changed, on artwork no synthetic fixture
resembles.

They move whenever a check legitimately improves, and that is the point: the
diff is the review. Several real bugs in this project were found by reading one.
Regenerate deliberately:

```
PCBDFM_UPDATE_BASELINES=1 pytest tests/test_golden.py
```

## Considered and rejected

Not vendored, on licence grounds — listed so the decision isn't re-litigated:

| source | licence | why not |
|---|---|---|
| jaseg/7segstuff `chibi_2024` (KiCad) | CC BY-SA | share-alike |
| camchaney/handheld-cnc (Fusion 360) | CERN-OHL-W v2 | reciprocal |
| OregonStateMarsRover/2011 (PADS) | GPL v2 | reciprocal |
| tracespace issues #367 / #371 (EasyEDA, Allegro) | none stated | attachments to bug reports, no licence |

Also considered: **myriadrf/LimeSDR-QPCIe** (Altium, CC-BY 3.0 — an acceptable
licence). Skipped on size, not licence: 14 copper layers, ~3 MB compressed, and
it would add minutes to CI. It remains the best candidate if a heavyweight,
many-layer, slot-bearing board is ever wanted — ideally behind a slow marker.
