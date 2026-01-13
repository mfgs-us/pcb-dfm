from __future__ import annotations

from pathlib import Path
from ..ingest import ingest_gerber_zip
from ..geometry import build_board_geometry
from ..checks.definitions import load_check_definitions_for_ruleset
from .context import CheckContext
from .check_runner import get_check_runner, run_checks
from ..results import DfmResult, CategoryResult, SummaryCounts, ResultSummary, RunInfo, RulesetInfo, DesignInfo
from datetime import datetime


def run_dfm_on_gerber_zip(
    gerber_zip: Path,
    ruleset_id: str,
    design_id: str = "board",
) -> DfmResult:
    """
    High level entry point:

    - Loads all CheckDefinition objects for the given ruleset
      (usually all built in checks for that ruleset).
    - Runs them in one pass over the Gerber zip.
    - Aggregates into a DfmResult.
    """
    check_defs = load_check_definitions_for_ruleset(ruleset_id)
    check_results = run_checks(
        gerber_zip=gerber_zip,
        check_defs=check_defs,
        ruleset_id=ruleset_id,
        design_id=design_id,
    )
    dfm_result = aggregate_check_results(check_results, ruleset_id, design_id)
    return dfm_result


def aggregate_check_results(check_results, ruleset_id: str, design_id: str) -> DfmResult:
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
    
    # Create CategoryResult objects
    category_results = []
    for cat_id, checks in categories.items():
        # Determine category status and score
        cat_status = "pass"
        cat_score = 100.0
        cat_violations = 0
        
        for check in checks:
            if check.status == "fail":
                cat_status = "fail"
                cat_score = min(cat_score, 0.0)
            elif check.status == "warning":
                cat_status = "warning"
                cat_score = min(cat_score, 75.0)
            cat_violations += len(check.violations)
        
        category_results.append(CategoryResult(
            category_id=cat_id,
            name=None,  # Could be derived from categories.json
            status=cat_status,
            score=cat_score,
            violations_count=cat_violations,
            checks=checks
        ))
    
    # Determine overall status and score
    overall_status = "pass"
    overall_score = 100.0
    
    for cat in category_results:
        if cat.status == "fail":
            overall_status = "fail"
            overall_score = min(overall_score, 0.0)
        elif cat.status == "warning":
            overall_status = "warning"
            overall_score = min(overall_score, 75.0)
    
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
            source_files=[str(gerber_zip)] if isinstance(gerber_zip, Path) else [gerber_zip],
            board_size_mm=None
        ),
        summary=summary,
        categories=category_results
    )

