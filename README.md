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

```
pcb-dfm/
├── checks/
│   ├── index.json                 # Master index of all checks
│   ├── acid_trap_angle.json
│   └── copper_density_balance.json
├── schemas/
│   └── pcb_dfm_result.schema.json # JSON Schema for DfmResult
├── pcb_dfm/
│   ├── __init__.py
│   ├── io.py                     # load_dfm_result / save_dfm_result
│   ├── results.py                # DfmResult, CategoryResult, CheckResult
│   ├── ingest/
│   │   ├── __init__.py
│   │   └── gerber_zip.py         # ingest_gerber_zip: Gerber.zip -> BoardGeometry
│   ├── geometry/
│   │   ├── __init__.py
│   │   └── model.py              # BoardGeometry, LayerGeometry, etc.
│   └── engine/
│       ├── __init__.py
│       ├── context.py            # CheckContext
│       ├── check_defs.py         # CheckDefinition model
│       └── check_runner.py       # run_single_check, registry
├── scripts/
│   ├── build_index.py            # Rebuild checks/index.json
│   ├── test_ingest.py            # Debug Gerber ingest
│   ├── test_geometry_stub.py     # Debug geometry extraction
│   ├── test_report.py            # Sample report generation
│   └── run_single_check.py       # Run one check on Gerber.zip
├── testdata/
│   └── Gerbers.zip              # Example board
├── README.md
├── pyproject.toml
└── LICENSE
```

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

```python
from pathlib import Path
from pcb_dfm.engine.check_defs import CheckDefinition
from pcb_dfm.engine.check_runner import run_single_check

zip_path = "Gerbers.zip"
check_id = "min_trace_width"

# Load the check definition
check_json_path = Path("checks") / f"{check_id}.json"
check_def = CheckDefinition.model_validate_json(check_json_path.read_text())

# Run the check
result = run_single_check(zip_path, check_def)

# Use the result
print(f"{result.check_id}: {result.status} (Score: {result.score})")
print(result.metric)
for v in result.violations:
    print(f"{v.severity}: {v.message} at {v.location}")
```

### 5.2 Loading and Saving Full DFM Results

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

1. Create a new file in `checks/` (e.g., `my_new_check.json`)
2. Fill it using existing checks as templates
3. Rebuild the index:
   ```bash
   py -3 scripts/build_index.py
   ```

### 7.2 Implement the Check Logic in Python

Create `pcb_dfm/checks/impl_my_new_check.py`:

```python
from __future__ import annotations
from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation

@register_check("my_new_check")
def run_my_new_check(ctx: CheckContext) -> CheckResult:
    """Implementation of the check logic."""
    metric_cfg = ctx.check_def.metric or {}
    # Your check logic here
    # Access geometry: ctx.geometry
    # Access check config: ctx.check_def.raw
    # Return a CheckResult
```

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