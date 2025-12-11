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

- Python 3.10 or newer
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

## 3. Quickstart: Run a Single Check from CLI

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
  "severity": "error",
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