from __future__ import annotations

from .results import DfmResult, CategoryResult, CheckResult


def summarize_status(counts) -> str:
    return (
        f"info: {counts.info}, "
        f"warning: {counts.warning}, "
        f"error: {counts.error}, "
        f"critical: {counts.critical}"
    )


def generate_text_report(result: DfmResult) -> str:
    lines = []

    lines.append(f"DFM Report for {result.design.name}")
    if result.design.revision:
        lines.append(f"Revision: {result.design.revision}")
    lines.append(f"Ruleset:  {result.ruleset.name} {result.ruleset.version}")
    lines.append("")
    lines.append(
        f"Overall status: {result.summary.status.upper()} "
        f"(score {result.summary.overall_score:.1f})"
    )
    lines.append(
        f"Total violations: {result.summary.violations_total} "
        f"({summarize_status(result.summary.violations_by_severity)})"
    )
    lines.append("")

    for cat in result.categories:
        lines.append(
            f"[{cat.category_id}] {cat.name or ''} - "
            f"status: {cat.status or 'n/a'}, "
            f"score: {cat.score if cat.score is not None else 'n/a'}, "
            f"violations: {cat.violations_count}"
        )
        for check in cat.checks:
            lines.append(f"  - {check.check_id}: {check.status} ({check.severity})")
            if check.metric and check.metric.measured_value is not None:
                mv = check.metric.measured_value
                units = check.metric.units or ""
                lines.append(f"      measured: {mv} {units}")
            if check.violations:
                first = check.violations[0]
                lines.append(f"      first violation: {first.message}")
        lines.append("")

    return "\n".join(lines)


def generate_markdown_report(result: DfmResult) -> str:
    lines = []

    lines.append(f"# DFM report - {result.design.name}")
    if result.design.revision:
        lines.append(f"_Revision: {result.design.revision}_")
    lines.append("")
    lines.append(f"- Ruleset: **{result.ruleset.name} {result.ruleset.version}**")
    lines.append(
        f"- Overall status: **{result.summary.status.upper()}** "
        f"(score **{result.summary.overall_score:.1f}**)"
    )
    lines.append(
        f"- Total violations: **{result.summary.violations_total}** "
        f"({summarize_status(result.summary.violations_by_severity)})"
    )
    lines.append("")

    for cat in result.categories:
        lines.append(f"## {cat.name or cat.category_id}")
        lines.append("")
        lines.append(
            f"- Category id: `{cat.category_id}`  \n"
            f"- Status: **{cat.status or 'n/a'}**  \n"
            f"- Score: **{cat.score if cat.score is not None else 'n/a'}**  \n"
            f"- Violations: **{cat.violations_count}**"
        )
        lines.append("")
        lines.append("| Check id | Status | Severity | Score | Violations |")
        lines.append("|----------|--------|----------|-------|-----------|")
        for check in cat.checks:
            score = "" if check.score is None else f"{check.score:.1f}"
            lines.append(
                f"| `{check.check_id}` | {check.status} | {check.severity} | "
                f"{score} | {len(check.violations)} |"
            )
        lines.append("")

    return "\n".join(lines)
