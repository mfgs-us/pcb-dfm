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
| `rf_protoboard.kicad_pcb` | RF prototype board (KiCad 7) | BSD 3-clause | [maelh/radio-frequency-prototype-boards](https://github.com/maelh/radio-frequency-prototype-boards) `RF_ProtoBoard` |
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

**`rf_protoboard.kicad_pcb`** is different in kind: a native KiCad source file,
not Gerbers. With no `kicad-cli` installed the engine renders it with gerbonara,
so `geometry_source` is a render of the design, not the user's own fabrication
output — it answers "is this design manufacturable", not "is this package
correct". Its baseline therefore depends on the gerbonara version and is less
stable than the Gerber boards; the native parser is also fragile across KiCad
versions (see issues #26/#27). It is kept as the corpus's one native-KiCad /
non-Gerber entry, deliberately singular for that reason.

## Regression baselines

Each board here has a committed golden digest under `tests/baselines/corpus/`,
covering all 49 checks' status and measured value, produced with the board's own
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

Also considered, skipped on **size/speed, not licence** (welcome behind a slow
marker):

- **myriadrf/LimeSDR-QPCIe** (Altium, CC-BY 3.0). 14 copper layers, ~20k
  polygons/layer; times out (>2.5 min) even trimmed to two layers (issue #26).
  The best candidate if a heavyweight, many-layer, slot-bearing board is wanted.

Also considered, skipped on **cross-platform reproducibility** — the harder
wall. gerbonara 1.5.0 parses *complex/panelized* boards to subtly
platform-dependent geometry: the golden digest differs between local (macOS) and
CI (Linux) despite an identical gerbonara version, so no stable baseline can be
committed. This is **not** limited to a vendor format — it bites standard
RS-274X too — and it is not float noise absorbable by rounding (e.g. Fritzing's
`min_annular_ring` reads 0.352 mm locally vs 0.316 mm on CI, and a Linux baseline
would simply fail on macOS instead). The existing corpus boards are simple enough
to parse identically on both platforms; adding a complex board would need its
baseline generated on the same OS the tests run on.

- **ohguma/analog_gyro_2021** (Fritzing, MIT, standard RS-274X). Clean licence;
  #26 fixed the speed (62 s → ~14 s) — but a panel, and platform-divergent as
  above (issue #30 / PR closed).
- **yghdj/lasmo** (Siemens/Mentor, MIT). Validates fine locally, and its
  non-plated drill would exercise `npth_to_copper_clearance` on real artwork —
  but same platform divergence in gerbonara's parse of the Mentor `.gdo`/`.ncd`
  files.
