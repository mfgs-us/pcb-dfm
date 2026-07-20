from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..checks.definitions import load_check_definitions_for_ruleset
from ..geometry import build_board_geometry
from ..geometry.gerber_compat import rU_open_shim
from ..ingest import ingest_gerber_zip
from ..results import (
    CategoryResult,
    CheckResult,
    DesignInfo,
    DfmResult,
    ResultSummary,
    RulesetInfo,
    RunInfo,
    SummaryCounts,
)
from .check_runner import get_check_runner, run_checks
from .context import CheckContext
from .geometry_cache import GeometryCache


def run_dfm_on_gerber_zip(
    gerber_zip: Path,
    ruleset_id: str,
    design_id: str = "board",
    design_data=None,
) -> DfmResult:
    """
    High level entry point:

    - Loads all CheckDefinition objects for the given ruleset
      (usually all built in checks for that ruleset).
    - Runs them in one pass over the Gerber zip.
    - Aggregates into a DfmResult.
    """
    # Ingest once up front so we can both run checks against it AND report the
    # detected copper stackup / warnings (so a dropped inner layer is visible).
    with rU_open_shim():
        ingest_result = ingest_gerber_zip(gerber_zip)

    check_defs = load_check_definitions_for_ruleset(ruleset_id)
    check_results = run_checks(
        gerber_zip=gerber_zip,
        check_defs=check_defs,
        ruleset_id=ruleset_id,
        design_id=design_id,
        design_data=design_data,
        prebuilt_ingest=ingest_result,
    )
    dfm_result = aggregate_check_results(check_results, ruleset_id, design_id, gerber_zip)

    copper_layers, warnings = describe_stackup(ingest_result)
    dfm_result.design.stackup_layers = len(copper_layers)
    dfm_result.design.layers = copper_layers
    dfm_result.warnings = warnings
    return dfm_result


_COPPER_ORDER = {"TopCopper": 0, "BottomCopper": 9_999}


def describe_stackup(ingest_result):
    """Return (copper_layers, warnings): an ordered, human-readable copper
    stackup (top -> inner1..N -> bottom, each 'Layer: filename') and any ingest
    warnings (e.g. an unclassified copper-looking file)."""
    def _rank(f):
        if f.logical_layer == "TopCopper":
            return -1
        if f.logical_layer == "BottomCopper":
            return 10_000
        m = re.search(r"InnerCopper(\d+)", f.logical_layer or "")
        return int(m.group(1)) if m else 5_000

    copper = sorted(
        (f for f in ingest_result.files if f.layer_type == "copper"),
        key=_rank,
    )
    layers = [f"{f.logical_layer}: {f.original_name}" for f in copper]
    return layers, list(getattr(ingest_result, "warnings", []) or [])


def build_geometry_for(gerber_zip: Path):
    """Ingest + build the board geometry for a Gerber zip (for rendering).

    Runs inside the pcb-tools shim. Independent of check execution; used by the
    HTML report so it can draw the board.
    """
    with rU_open_shim():
        ingest_result = ingest_gerber_zip(gerber_zip)
        return build_board_geometry(ingest_result)


def run_dfm_bundle(
    gerber_zip: Path,
    ruleset_id: str = "default",
    design_id: str = "board",
    design_data=None,
) -> dict:
    """
    Run a full DFM pass and return a plain-dict summary suitable for a JSON
    API boundary. Never raises: any failure is captured in ``error``.

    Contract::

        {
          "overall_score": float,          # 0..100
          "check_results": [ {...}, ... ], # issues only (warning/fail), as dicts
          "stats": {"total", "passed", "warnings", "failed"},
          "error": None | str,
        }
    """
    empty_stats = {"total": 0, "passed": 0, "warnings": 0, "failed": 0}
    try:
        from ..checks import _ensure_impls_loaded
        from ..ingest.design_data import load_design_data
        _ensure_impls_loaded()
        design_data = load_design_data(design_data)

        check_defs = load_check_definitions_for_ruleset(ruleset_id)

        with rU_open_shim():
            ingest_result = ingest_gerber_zip(gerber_zip)
            geom = build_board_geometry(ingest_result)
        cache = GeometryCache()

        stats = {"total": 0, "passed": 0, "warnings": 0, "failed": 0}
        issues: list[dict] = []

        for check_def in check_defs:
            stats["total"] += 1
            try:
                runner = get_check_runner(check_def.id)
            except KeyError:
                # Unimplemented check: not applicable, not an error.
                continue

            ctx = CheckContext(
                check_def=check_def,
                ingest=ingest_result,
                geometry=geom,
                geometry_cache=cache,
                ruleset_id=ruleset_id,
                design_id=design_id,
                gerber_zip=gerber_zip,
            )

            try:
                with rU_open_shim():
                    result = runner(ctx)
            except Exception as exc:
                # A crash is a failure, never a silent pass.
                stats["failed"] += 1
                issues.append({
                    "check_id": check_def.id,
                    "status": "fail",
                    "score": 0.0,
                    "violations": [{
                        "severity": "error",
                        "message": f"Check crashed: {type(exc).__name__}: {exc}",
                    }],
                })
                continue

            if isinstance(result, CheckResult):
                result = result.finalize()

            status = getattr(result, "status", None)
            if status == "pass":
                stats["passed"] += 1
            elif status == "warning":
                stats["warnings"] += 1
            elif status == "fail":
                stats["failed"] += 1

            # Issues-only payload (skip passes / not_applicable).
            if status in ("warning", "fail"):
                issues.append(_result_to_dict(result))

        total = stats["total"]
        if total > 0:
            overall_score = 100.0 * (stats["passed"] + 0.5 * stats["warnings"]) / total
        else:
            overall_score = 0.0

        return {
            "overall_score": overall_score,
            "check_results": issues,
            "stats": stats,
            "error": None,
        }
    except Exception as exc:
        return {
            "overall_score": 0.0,
            "check_results": [],
            "stats": dict(empty_stats),
            "error": str(exc),
        }


def _result_to_dict(result) -> dict:
    if hasattr(result, "to_dict"):
        return result.to_dict()
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return dict(result)


def aggregate_check_results(
    check_results,
    ruleset_id: str,
    design_id: str,
    gerber_zip: Path | str | None = None,
) -> DfmResult:
    """Aggregate individual CheckResult objects into a DfmResult."""

    # Group results by category
    categories: dict[str, list[CheckResult]] = {}
    summary_counts = SummaryCounts()

    for result in check_results:
        cat_id = result.category_id or "other"
        if cat_id not in categories:
            categories[cat_id] = []
        categories[cat_id].append(result)

        # Count violations by severity
        for violation in result.violations:
            if violation.severity == "info":
                summary_counts.info += 1
            elif violation.severity == "warning":
                summary_counts.warning += 1
            elif violation.severity == "error":
                summary_counts.error += 1
            elif violation.severity == "critical":
                summary_counts.critical += 1

    # Rank so the WORST status wins regardless of iteration order. The old
    # code was last-write-wins, so a warning check after a fail check would
    # downgrade the status back to "warning" while the score stayed 0.
    _status_rank = {"pass": 0, "not_applicable": 0, "warning": 1, "fail": 2}
    _rank_status = {0: "pass", 1: "warning", 2: "fail"}

    # Create CategoryResult objects
    category_results = []
    for cat_id, checks in categories.items():
        cat_rank = 0
        cat_score = 100.0
        cat_violations = 0

        for check in checks:
            cat_rank = max(cat_rank, _status_rank.get(check.status, 0))
            if check.status == "fail":
                cat_score = min(cat_score, 0.0)
            elif check.status == "warning":
                cat_score = min(cat_score, 75.0)
            cat_violations += len(check.violations)

        category_results.append(CategoryResult(
            category_id=cat_id,
            name=None,  # Could be derived from categories.json
            status=_rank_status[cat_rank],
            score=cat_score,
            violations_count=cat_violations,
            checks=checks
        ))

    # Determine overall status and score (worst-wins, same rank logic).
    overall_rank = 0
    overall_score = 100.0

    for cat in category_results:
        overall_rank = max(overall_rank, _status_rank.get(cat.status or "pass", 0))
        if cat.status == "fail":
            overall_score = min(overall_score, 0.0)
        elif cat.status == "warning":
            overall_score = min(overall_score, 75.0)
    overall_status = _rank_status[overall_rank]

    # Create summary
    summary = ResultSummary(
        overall_score=overall_score,
        status=overall_status,
        violations_total=sum(cat.violations_count for cat in category_results),
        violations_by_severity=summary_counts
    )

    # Create DfmResult
    return DfmResult(
        schema_version="1.0.0",
        run=RunInfo(
            id="dfm-run",
            generated_at=datetime.now(),
            tool="pcb-dfm",
            tool_version="1.0.0"
        ),
        ruleset=RulesetInfo(
            name=ruleset_id,
            version="1.0.0"
        ),
        design=DesignInfo(
            name=design_id,
            revision=None,
            source_files=[str(gerber_zip)] if gerber_zip is not None else [],
            board_size_mm=None
        ),
        summary=summary,
        categories=category_results
    )

