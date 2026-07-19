# Contributing to pcb-dfm

Thanks for helping improve the DFM engine. This guide covers the dev setup, how
the pieces fit together, and how to add a new check.

## Development setup

```bash
python -m pip install -e ".[dev]"   # pydantic, pcb-tools, pytest, mypy, jsonschema
pytest -q                           # run the test suite
```

A small runnable board ships at `testdata/mini_board.zip`, so the tests and the
CLI work from a clean clone with no extra input.

```bash
pcb-dfm run testdata/mini_board.zip --format text   # or: python -m pcb_dfm run ...
pcb-dfm list-checks
```

## Layout

- `pcb_dfm/check_data/checks/*.json` — check **definitions** (thresholds, units).
- `pcb_dfm/checks/impl_*.py` — check **implementations**, registered via
  `@register_check("<id>")`. The JSON `id` must match the id passed to the
  decorator.
- `pcb_dfm/engine/` — orchestration (`run.py`), the runner + registry
  (`check_runner.py`), and the execution `CheckContext`.
- `pcb_dfm/geometry/`, `pcb_dfm/ingest/` — Gerber/Excellon parsing and the
  polygon geometry model.
- `pcb_dfm/results.py` — the Pydantic result models (`CheckResult`,
  `MetricResult`, `Violation`, …) and the finalize/severity invariants.

## Adding a check

1. Add `pcb_dfm/check_data/checks/<id>.json` (copy an existing one as a
   template; set `id`, `category_id`, `metric`, `limits`, `raw`).
2. Add `pcb_dfm/checks/impl_<id>.py`:

   ```python
   from ..engine.check_runner import register_check
   from ..engine.context import CheckContext
   from ..results import CheckResult, MetricResult

   @register_check("<id>")
   def run_<id>(ctx: CheckContext) -> CheckResult:
       ...
       return CheckResult(check_id=ctx.check_def.id, status="pass", ...)
   ```

3. Register it by adding an import in `pcb_dfm/checks/__init__.py`
   (`_ensure_impls_loaded`).

### Rules of the road for checks

- **Never fabricate a measurement.** If the data you need isn't present
  (no copper, no drills, no stackup), return `status="not_applicable"` with a
  message explaining what was missing.
- **Use the framework:** return a `CheckResult` (the runner calls `finalize()`).
  Build metrics with the `MetricResult` constructors — `geometry_mm(...)` (mm,
  tolerates `None`), `ratio_percent(...)`/`ratio_min_percent(...)` (`%`), and
  `dimensionless(...)` (unit-less, e.g. aspect ratios). Geometry metrics must be
  in mm, ratios in `%`.
- **Data model:** `ctx.geometry` exposes `.layers`, `.get_layer(...)`,
  `.get_layers_by_type(...)`, `.board_bounds()`. There is no `.board` or
  `.backdrilled_vias`; `ctx` has no `.rules` — configuration comes from
  `ctx.check_def` (`.raw`, `.limits`, `.metric`).
- **Wrap pcb-tools use in `try/except`** and degrade gracefully; crashes are
  surfaced by the runner as a visible `fail`, never a silent pass.

## Before opening a PR

```bash
pytest -q
ruff check .        # if configured
mypy pcb_dfm        # if configured
```

Keep changes minimal and match the surrounding style.
