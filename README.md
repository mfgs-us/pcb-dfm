<p align="center">
  <img src="assets/banner.svg" alt="pcb-dfm — schema-driven Design-for-Manufacturing engine for PCB Gerbers" width="900">
</p>

<p align="center">
  <a href="https://github.com/mfgs-us/pcb-dfm/actions/workflows/ci.yml"><img src="https://github.com/mfgs-us/pcb-dfm/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

# PCB Design for Manufacturing (DFM) Engine

`pcb-dfm` is a schema-driven Design For Manufacturability (DFM) engine for PCB Gerbers.

- **Input**: Gerber `.zip` (copper, mask, silkscreen, outline, drills, etc.)
- **Rules**: JSON definitions in `checks/*.json` + `categories.json`
- **Output**: Structured DFM results (scores, metrics, violations) + human-readable summaries

The project is designed to be used both as a Python library (importable module) and through CLI helper scripts in `./scripts`.

## Table of Contents
- [Installation](#1-installation)
  - [Requirements](#11-requirements)
  - [Clone the Repository](#12-clone-the-repository)
- [Project Layout](#2-project-layout)
  - [Recent Improvements: Integr8tor Alignment & Consistency Fixes](#21-recent-improvements-integr8tor-alignment--consistency-fixes)
- [Quickstart](#3-quickstart-run-a-single-check-from-cli)
- [Debug Helpers](#4-debug-helpers)
  - [Inspect Gerber Ingest](#41-inspect-gerber-ingest)
  - [Inspect Geometry and Polygons](#42-inspect-geometry-and-polygons)
- [Using as a Python Module](#5-using-pcb-dfm-as-a-python-module)
  - [Running a Single Check](#51-running-a-single-check-programmatically)
  - [Loading and Saving Results](#52-loading-and-saving-full-dfm-results)
- [Rule and Check Schema](#6-rule-and-check-schema)
- [Adding or Editing Checks](#7-adding-or-editing-checks)
  - [Add a New Check Definition](#71-add-a-new-check-definition-json)
  - [Implement Check Logic](#72-implement-the-check-logic-in-python)
- [Typical Workflow](#8-typical-workflow)

## 1. Installation

### 1.1 Requirements

- Python 3.9 or newer (CI runs 3.9–3.11)
- `pip`
- Git

### Dependencies
- `pydantic` v2 for data models
- `pcb-tools` (Gerber/Excellon parsing)
- Standard library only otherwise

### 1.2 Clone the Repository

```bash
git clone https://github.com/your-org-or-user/pcb-dfm.git
cd pcb-dfm
```

### 1.3 Install in Development Mode

On Windows:
```bash
py -3 -m pip install -e .
```

On Linux/macOS:
```bash
python3 -m pip install -e .
```

This installs `pcb_dfm` as an editable package, so changes to the repository are picked up immediately.

## 2. Project Layout

```text
pcb-dfm/
├── pcb_dfm/
│   ├── __init__.py
│   ├── io.py                      # load_dfm_result / save_dfm_result
│   ├── results.py                 # DfmResult, CategoryResult, CheckResult, Violation, etc.
│   ├── ingest/
│   │   ├── __init__.py
│   │   └── gerber_zip.py          # ingest_gerber_zip: Gerber.zip -> raw layer data
│   ├── geometry/
│   │   ├── __init__.py
│   │   └── model.py               # BoardGeometry, LayerGeometry, primitives, queries, etc.
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── context.py             # CheckContext
│   │   ├── check_runner.py        # run_single_check, registry
│   │   └── run.py                 # run_dfm_on_gerber_zip, orchestration
│   ├── checks/
│   │   ├── __init__.py
│   │   ├── definitions.py         # CheckDefinition, load_check_definition, load_all_check_definitions
│   │   ├── impl_plane_fragmentation.py
│   │   ├── impl_min_trace_width.py
│   │   └── ...                    # other impl_* check modules
│   └── check_data/
│       └── checks/
│           ├── index.json         # Optional index of all checks / rulesets
│           ├── plane_fragmentation.json
│           ├── min_trace_width.json
│           └── ...               # other check definition JSON files
├── scripts/
│   ├── build_index.py             # Rebuild check_data/checks/index.json
│   ├── run_single_check.py        # Run one check on Gerber.zip by id
│   ├── test_ingest.py             # Debug Gerber ingest
│   ├── test_geometry_stub.py      # Debug geometry extraction
│   ├── test_report.py             # Sample report generation
│   ├── test_custom_check_from_file.py      # Run a check using id or JSON path
│   └── test_custom_check_equivalence.py    # Assert id vs JSON path give same result
├── testdata/
│   └── Gerbers.zip               # Example board
├── schemas/
│   └── pcb-dfm.ruleset.schema.json # JSON Schema for check definitions
├── README.md
├── pyproject.toml
└── LICENSE
```

This makes the difference between:

- where JSON lives (`check_data/checks`)  
- where Python check implementations live (`checks/impl_*.py`)  

explicit.

## 2.1 Recent Improvements: Integr8tor Alignment & Consistency Fixes

> **Scope note.** Integr8tor alignment is a **design goal**, not a certification.
> This engine is artwork-only (no netlist/stackup input yet), so several checks
> are deliberately heuristic, and some are honest `not_applicable` placeholders
> until connectivity/stackup data is wired in (see the coverage note at the end
> of this section). The items below describe changes that have landed, but treat
> the numeric thresholds as tunable defaults rather than a manufacturer spec.

This version includes major improvements to align with industry-standard tools like Integr8tor and fix fundamental consistency issues:

### 🛡️ Reliability & Correctness Overhaul (latest)

A pass over the engine closed a class of bugs where a defective board could be
reported as clean. The whole pipeline now runs end-to-end and is covered by a
committed fixture (`testdata/mini_board.zip`) and a passing test suite.

**A crash can never look like a pass**
- ✅ **Crashes surface as failures**: if a check raises, the runner records a
  `status="fail"` with an `error` violation (`Check crashed: …`) — never the
  old silent `not_applicable` / score 100. The rest of the batch keeps running.
- ✅ **Missing implementations don't abort the run**: definitions with no
  registered check are reported as `not_applicable` instead of throwing an
  uncaught `KeyError` that killed the whole batch.
- ✅ **Consistent finalization**: `run_single_check` now finalizes like the
  batch path, so the single-check path can no longer emit `pass` + `error`.

**Flagship check crashes fixed** (these crashed *only on violating boards*, so
they used to hide real defects):
- ✅ Unbound-`severity` `NameError` in `min_trace_width`, `min_drill_size`,
  `drill_aspect_ratio`, `via_to_copper_clearance`.
- ✅ Tuple-unpack `ValueError` in `via_in_pad_thermal_balance`.

**High-level entry points work**
- ✅ `run_dfm_on_gerber_zip` fixed (aggregation no longer references an
  undefined variable) and `run_dfm_bundle` implemented (issues-only results +
  `stats` + `error` contract).
- ✅ **Worst-status-wins aggregation**: a `warning` after a `fail` no longer
  downgrades a category/overall status, so `status` and `score` can't disagree.

**Parsing / ingest hardening**
- ✅ **Aperture minimum-feature check** compares the *minimum* dimension (was
  `max`, which never caught slivers); a failed unit conversion no longer
  inflates mm dimensions ~25×.
- ✅ **Outline fallback parser** splits contours on pen-up (`D02`) moves and
  carries modal coordinates, instead of merging disjoint loops into one polygon.
- ✅ **Zip-slip / zip-bomb guards** on archive extraction.
- ✅ **Loud degradation**: a missing `pcb-tools` now warns instead of silently
  producing empty geometry (which made every check pass vacuously).

**Honest, not fabricated, results**
- ✅ Removed a hard-coded fabricated `0.296 mm` measurement in
  `copper_to_edge_distance`.
- ✅ Checks that need data not present in Gerbers (backdrill depth, stackup,
  impedance constraints) return an honest `not_applicable` with an explanation
  instead of keying off attributes that never existed.

**Tooling & hygiene**
- ✅ **Working CLI**: `pcb-dfm run|check|list-checks` (and `python -m pcb_dfm`);
  `[DFM TIMING]` diagnostics moved to stderr so stdout stays clean for JSON.
- ✅ `pcb_dfm.io` is a proper package again (no module/package shadowing); the
  result **JSON schema** is realigned to the models and the sample validates.
- ✅ Metric labeling fixed: aspect ratios report `:1` (not `%`); via-tenting
  margin is no longer inverted; violation `location.notes` is preserved.

### 🎯 Geometric Accuracy Improvements

**Annular Ring Check (`impl_min_annular_ring.py`)**
- ✅ **Non-plated drill filtering**: Uses ingest metadata to skip non-plated drills
- ✅ **Point-in-polygon containment**: Replaced bbox approximation with geometric testing
- ✅ **True edge-distance measurement**: Computes actual ring distance from drill edge to copper edge
- ✅ **Pad candidate filtering**: Filters pads by aspect ratio, size, and area to eliminate false positives
- ✅ **Proper unit detection**: Fixed Excellon drill unit handling to prevent silent double conversion

**Solder Mask Expansion (`impl_solder_mask_expansion.py`)**
- ✅ **Mask polarity normalization**: Automatically detects openings vs coverage using 50% board area heuristic
- ✅ **True distance measurement**: Replaced bbox approximation with geometric distance calculation
- ✅ **Coverage-to-openings inversion**: Framework for handling coverage-based mask polygons

### 🏗️ Layer Classification Reliability (`gerber_zip.py`)

**Extension-First Classification**
- ✅ **Reliable detection**: Uses extensions (.gtl, .gbl, .gts, .gbs, .gto, .gbo) before name heuristics
- ✅ **Enhanced outline detection**: Improved recognition of .gbr files like "Edge_Cuts.gbr"
- ✅ **Selective fallback parsing**: Only parses outline geometry from strongly-indicated files
- ✅ **Clean outline extraction**: Filters non-outline moves and uses D01 commands only

### ⚖️ Policy Alignment with Integr8tor

**Severity Policy Changes**
- ✅ **Silkscreen over copper**: Defaults to warning (CAM clipping assumed)
- ✅ **Copper density balance**: Defaults to info/warning instead of fail (Integr8tor doesn't report)
- ✅ **Via tenting ratio**: Defaults to warning/info instead of fail (Integr8tor doesn't enforce)

**Opt-in Strict Modes**
- ✅ **`strict_plating_mode`**: Enable copper density failures for plating risk profiles
- ✅ **`strict_assembly_mode`**: Enable via tenting failures for assembly risk profiles
- ✅ **`fab_clips_silkscreen`**: Control silkscreen strictness (default: True)

### 🔧 System-Wide Consistency Fixes

**Metric Units & Status Consistency**
- ✅ **Single source of truth**: `MetricResult.geometry_mm()` and `MetricResult.ratio_percent()` constructors
- ✅ **Unit validation**: Enforces "mm" for geometry, "%" for ratios, prevents scale mismatches
- ✅ **Severity contract**: `pass→info`, `warning→warning`, `fail→error` unless violations justify otherwise
- ✅ **Auto score calculation**: Consistent scoring based on status (pass=100, warning=75, fail=0)
- ✅ **Margin calculation**: Automatic computation of `margin_to_limit` with correct sign

**Before/After Examples**
```json
// Before (inconsistent)
{
  "status": "warning",
  "severity": "error",  // Wrong unless error violations exist
  "metric": {
    "units": "um",      // Wrong - 0.296 value is mm scale
    "measured_value": 0.296
  }
}

// After (consistent)
{
  "status": "pass",
  "severity": "info",   // Correct - derived from status
  "metric": {
    "units": "mm",      // Correct - matches value scale
    "measured_value": 0.296
  }
}
```

### 📊 Expected Results

These improvements transform PCB-DFM from producing false failures to providing accurate, Integr8tor-aligned measurements:

- **Annular ring**: True geometric measurements instead of zero/false failures
- **Solder mask expansion**: Correct polarity interpretation and distance calculation  
- **Copper-to-edge clearance**: Reliable outline geometry and consistent units
- **Policy alignment**: Lenient approach for non-critical issues, strict when opted-in
- **System consistency**: No more unit contradictions or severity/status mismatches

### 🔧 MetricResult System: Single Source of Truth

PCB-DFM now enforces global consistency through a unified `MetricResult` system:

**Metric Constructors:**
```python
# Geometry metrics (distances, sizes). measured_mm may be None to mean
# "could not measure" (e.g. no copper present) without raising.
MetricResult.geometry_mm(
    measured_mm=0.296,
    target_mm=0.25,
    limit_low_mm=0.15
)

# Ratio metrics where higher-is-worse and the bound is a maximum (%).
MetricResult.ratio_percent(
    measured_pct=96.6,
    target_pct=20.0,
    limit_high_pct=30.0
)

# Ratio metrics where higher-is-better and the bound is a minimum (%),
# e.g. via tenting — margin = measured - limit_low (positive when good).
MetricResult.ratio_min_percent(
    measured_pct=90.0,
    target_pct=80.0,
    limit_low_pct=50.0
)

# Unit-less metrics such as an aspect ratio (renders as ":1", not "%").
MetricResult.dimensionless(
    measured=8.0,
    target=8.0,
    limit_high=10.0
)
```

**Clean Invariant:**
- **If violations empty** → severity derived from status only
  - `pass/not_applicable` → `info`
  - `warning` → `warning` 
  - `fail` → `error`
- **If violations exist** → severity = max(violation severities)

**Automatic Consistency:**
- ✅ Unit validation (geometry uses "mm", ratios use "%")
- ✅ Automatic margin calculation with correct sign
- ✅ Consistent scoring (pass=100, warning=75, fail=0)
- ✅ No more "pass + error" contradictions

**Check coverage:** all 30 implemented checks now run without crashing on the
bundled fixture. The 15 definitions that are not yet implemented (e.g.
`diff_pair_skew`, `min_slot_width`, `tombstoning_risk`) are reported as
`not_applicable` rather than aborting the run. Checks that require data absent
from Gerbers (`backdrill_stub_length`, `impedance_control`,
`dielectric_thickness_uniformity`) return `not_applicable`/`warning` with an
explanation until stackup/netlist input is wired in.

## 3. Quickstart: Run a Single Check from CLI

> A small runnable fixture ships with the repo at `testdata/mini_board.zip`, so
> you can try everything below without supplying your own Gerbers.

After `pip install -e .` you also get a `pcb-dfm` command (and `python -m pcb_dfm`):

```bash
pcb-dfm run testdata/mini_board.zip --format text     # full ruleset, text report
pcb-dfm run testdata/mini_board.zip --format json -o out/result.json
pcb-dfm run testdata/mini_board.zip --format html -o report.html  # visual report
pcb-dfm check testdata/mini_board.zip min_trace_width # single check, JSON to stdout
pcb-dfm list-checks                                    # list every check id
```

The `html` format writes a **single self-contained page** (no external assets):
the board rendered from its polygons with every located violation overlaid as a
severity-colored marker, alongside per-category findings that highlight their
marker on click. Light/dark aware.

Diagnostic `[DFM TIMING]` lines are written to stderr, so `stdout` stays clean
for JSON piping.

### Fab capability profiles (rulesets)

The same board is manufacturable at one fab and not another, so thresholds are
not universal. A **ruleset** is a named fab capability profile that both selects
which checks run and overrides their thresholds/policy:

```bash
pcb-dfm list-rulesets                                 # available profiles
pcb-dfm run testdata/mini_board.zip --ruleset advanced_hdi
pcb-dfm run testdata/mini_board.zip --ruleset conservative_2layer
pcb-dfm check board.zip min_trace_width --ruleset conservative_2layer
```

Starter profiles ship in `pcb_dfm/check_data/rulesets/`:

- **`default`** — every check, built-in default thresholds (back-compatible).
- **`advanced_hdi`** — fine-line HDI window (~3 mil trace/space, laser vias).
- **`conservative_2layer`** — economy 2-layer window (~5–6 mil, 0.2 mm drill);
  disables the high-speed SI category (no controlled-impedance service).

A profile (schema: `schemas/pcb-dfm.ruleset-profile.schema.json`) can:

- **select** checks — `enabled_checks` (whitelist), `disabled_checks`,
  `disabled_categories`;
- **override** any check — `overrides: { <check_id>: <partial check JSON> }`,
  deep-merged onto the base (e.g. inject a top-level `limits` block);
- set global **policy** flags injected into every check
  (`strict_plating_mode`, `fab_clips_silkscreen`, …);
- inherit via **`extends`**.

The shipped thresholds are illustrative starting points — tune them against a
specific fab's datasheet.

### Design data (stackup / netlist)

Some checks need information bare Gerbers don't carry — the layer stackup and
which features belong to which net. Supply it with `--design-data` and those
checks compute real results instead of reporting `not_applicable`:

```bash
pcb-dfm run testdata/mini_board.zip --design-data board.ipc2581.xml   # IPC-2581
pcb-dfm check testdata/mini_board.zip diff_pair_skew --design-data design.json
```

Two input formats are supported, both mapped onto one internal `DesignData`
model (`pcb_dfm/ingest/design_model.py`) so checks are format-agnostic:

- **IPC-2581** (`.xml`) — a documented subset: stackup layers (thickness + Er),
  logical nets, per-net routed length, controlled-impedance hints, and diff
  pairs (explicit or inferred from `_P`/`_N` naming). See
  `testdata/sample_design.xml`.
- **JSON sidecar** — a lightweight shape documented in
  `pcb_dfm/ingest/adapters/sidecar.py`.

This powers `impedance_control` (microstrip Z0 estimate), `diff_pair_skew`
(per-net length skew), `dielectric_thickness_uniformity`, and
`diff_pair_spacing` (intra-pair gap consistency from per-net routing geometry —
IPC-2581 `<Line>`/`<Arc>` segments, or sidecar net `segments`). Still to come:
Gerber-only net inference (tagging untagged copper by nearest net),
`return_path_interruptions`, and an **ODB++** adapter. When no design data is
supplied, these checks are honestly `not_applicable`.

1. Place your Gerber archive in the repository root:
   ```
   pcb-dfm/
     Gerbers.zip
     ...
   ```

2. Run a check (e.g., `min_trace_width`):
   ```bash
   # Windows
   py -3 .\scripts\run_single_check.py Gerbers.zip min_trace_width
   
   # Linux/macOS
   python3 scripts/run_single_check.py Gerbers.zip min_trace_width
   ```

Example output:
```json
{
  "check_id": "min_trace_width",
  "name": "Minimum trace width",
  "category_id": "copper_geometry",
  "status": "pass",
  "severity": "info",
  "score": 100.0,
  "metric": {
    "kind": "geometry",
    "units": "mm",
    "measured_value": 0.2,
    "target": 0.1,
    "limit_low": 0.075,
    "limit_high": null,
    "margin_to_limit": 0.125
  },
  "violations": []
}
```

### Notes:
- `status`: One of `pass`, `warning`, or `fail`
- `score`: 0 to 100, monotonic with severity and margin
- `metric.measured_value`: In mm or appropriate units
- `violations`: Contains specific locations and messages if issues exist

Run other checks:
```bash
py -3 scripts/run_single_check.py Gerbers.zip copper_to_edge_distance
py -3 scripts/run_single_check.py Gerbers.zip min_drill_size
py -3 scripts/run_single_check.py Gerbers.zip copper_density_balance
```

## 4. Debug Helpers

### 4.1 Inspect Gerber Ingest

Check how the Gerber archive is interpreted:
```bash
py -3 scripts/test_ingest.py
```

### 4.2 Inspect Geometry and Polygons

View polygon counts and board extents:
```bash
py -3 scripts/test_geometry_stub.py
```

## 5. Using as a Python Module

### 5.1 Running a Single Check Programmatically

You can run any check from Python using its JSON definition. The definition can be addressed either by id (for built in checks) or by an explicit JSON file path.

```python
from pathlib import Path
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check

zip_path = Path("Gerbers.zip")

# Option 1: load a built in check definition by id
check_def = load_check_definition("min_trace_width")

# Option 2: load a check definition from a specific JSON file
# (for example a custom or modified version)
# from importlib.resources import files
# checks_dir = Path(files("pcb_dfm").joinpath("check_data", "checks"))
# check_def = load_check_definition(checks_dir / "min_trace_width.json")

result = run_single_check(
    gerber_zip=zip_path,
    check_def=check_def,
    ruleset_id="default",   # string tag for your ruleset
    design_id="my_board_v1" # string tag for this board
)

print(f"{result.check_id}: {result.status} (Score: {result.score})")
print(result.metric)
for v in result.violations:
    loc = v.location
    if loc is not None:
        print(f"{v.severity}: {v.message} at {loc.layer} ({loc.x_mm:.3f}, {loc.y_mm:.3f})")
    else:
        print(f"{v.severity}: {v.message}")
```

### 5.2 Running a Check From a Definition File (id vs path)

Internally, each check is defined by a JSON file under `pcb_dfm/check_data/checks/*.json` and implemented by a Python function in `pcb_dfm/checks/impl_*.py`.

You can exercise a check in two equivalent ways:

- By id (uses the installed JSON definition)
- By explicit JSON path (useful for custom or experimental checks)

From the CLI:

```bash
# Run a built in check by id
py -3 scripts/run_single_check.py Gerbers.zip plane_fragmentation

# Run the same check by its JSON definition file
py -3 scripts/run_single_check.py ^
    Gerbers.zip ^
    pcb_dfm/check_data/checks/plane_fragmentation.json

# Assert that both runs produce the same CheckResult summary
py -3 scripts/test_custom_check_equivalence.py Gerbers.zip
```

Programmatically, you can do the same thing by passing either a string id or a Path to `load_check_definition` as shown in section 5.1.

### 5.3 Loading and Saving Full DFM Results

```python
from pcb_dfm.results import DfmResult
from pcb_dfm.io import save_dfm_result, load_dfm_result

# Save results
save_dfm_result(dfm_result, "out/dfm_result.json")

# Load results
loaded = load_dfm_result("out/dfm_result.json")
assert isinstance(loaded, DfmResult)
```

## 6. Rule and Check Schema

Example check definition (`checks/min_trace_width.json`):

```json
{
  "id": "min_trace_width",
  "category_id": "copper_geometry",
  "name": "Minimum trace width",
  "description": "Verify that copper traces meet the minimum manufacturable width.",
  "what_it_measures": "Smallest effective width of copper features classified as traces.",
  "metric": {
    "kind": "geometry",
    "units": "mm",
    "preferred_direction": "maximize",
    "target": { "min": 0.1 },
    "limits": { "min": 0.075 },
    "scale_min": 0.05,
    "scale_max": 0.3
  },
  "severity_default": "error",
  "applies_to": ["copper"],
  "scoring": {
    "enabled": true,
    "strategy": "linear",
    "pass_threshold": 80,
    "weight": 1.0
  },
  "raw": {
    "trace_min_area_mm2": 0.01,
    "trace_max_aspect_ratio": 20.0
  }
}
```

## 7. Adding or Editing Checks

### 7.1 Add a New Check Definition JSON

1. Create a new file in `pcb_dfm/check_data/checks/`, for example:

   ```text
   pcb_dfm/check_data/checks/my_new_check.json
   ```

2. Use an existing check as a template (for example `min_trace_width.json`) and adjust:
   - `id`: unique string id for the check (for example "my_new_check")
   - `category_id`: where it shows up in the report
   - `metric`, `applies_to`, `limits`, `raw`: numeric thresholds and configuration

3. Optionally rebuild the index:
   ```bash
   py -3 scripts/build_index.py
   ```

   This updates `pcb_dfm/check_data/checks/index.json` if you use a ruleset index.

### 7.2 Implement the Check Logic in Python

Create a new file under `pcb_dfm/checks/`, for example:

```text
pcb_dfm/checks/impl_my_new_check.py
```

and register your implementation using the shared registry:

```python
from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation


@register_check("my_new_check")
def run_my_new_check(ctx: CheckContext) -> CheckResult:
    """
    Implementation of the check logic.

    - ctx.check_def gives you the CheckDefinition (JSON config)
    - ctx.geometry gives you BoardGeometry and per layer polygons
    - ctx.ingest gives you the raw ingest data
    """
    metric_cfg = ctx.check_def.metric or {}
    raw_cfg = ctx.check_def.raw or {}

    # Your geometry analysis here...
    # For example:
    #   geom = ctx.geometry
    #   board = geom.board_outline
    #   ...

    # Construct and return a CheckResult
    # (see existing impl_* modules for patterns)
    ...
```

The important invariant is that the JSON definition's "id" must match the id you pass to `@register_check`. Once that is true, the engine can run the check either by id or by pointing at the JSON file.

### 7.3 Using Custom Checks From External JSON Files

You are not limited to JSON files inside the `pcb_dfm` package. For rapid experimentation you can keep definition files anywhere and use the same engine:

```bash
py -3 scripts/test_custom_check_from_file.py Gerbers.zip path/to/my_custom_check.json
```

`test_custom_check_from_file.py` will:
1. Detect if the second argument is an existing path or a check id
2. Load the corresponding CheckDefinition
3. Run the check using the engine and print a summary

This is the recommended way to iterate on new checks without touching the installed `pcb_dfm/check_data/checks` directory until you are happy with the behavior.

## 8. Typical Workflow

1. **Add Gerber Files**
   - Place `Gerbers.zip` in the repository root

2. **Debug Ingest and Geometry** (optional)
   ```bash
   py -3 scripts/test_ingest.py
   py -3 scripts/test_geometry_stub.py
   ```

3. **Run Individual Checks**
   ```bash
   py -3 scripts/run_single_check.py Gerbers.zip min_trace_width
   py -3 scripts/run_single_check.py Gerbers.zip min_trace_spacing
   ```

4. **Adjust Rule Thresholds**
   - Modify `metric.target`, `metric.limits`, and `raw` thresholds in the check JSON files

5. **Integrate with Other Tools**
   - Import `pcb_dfm` in your projects
   - Use `run_single_check` or other API functions

9. Limitations and assumptions
Current ingest path is Gerber centric:

.gtl, .gbl, .gko, .gts, .gbs, .gtp, .drl, etc.

Geometry representation is polygon and bbox based, in mm.

Some checks use heuristics:

Distinguishing traces vs pads vs planes

Guessing whether a pad is a via pad

Guessing silkscreen on copper via bbox overlaps

High speed, impedance, and some advanced checks may be partially stubbed or simplified by design for now.

As the engine matures, more checks will move to hybrid polygon plus netlist based implementations.

10. License
See LICENSE for details.