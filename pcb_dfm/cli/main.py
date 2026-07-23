"""
Command line entry point for pcb-dfm.

Examples::

    python -m pcb_dfm run Gerbers.zip --format text
    python -m pcb_dfm run Gerbers.zip --format json -o out/result.json
    python -m pcb_dfm check Gerbers.zip min_trace_width
    python -m pcb_dfm list-checks
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _cmd_run(args: argparse.Namespace) -> int:
    from ..engine.run import build_geometry_for, run_dfm_on_gerber_zip
    from ..report import (
        generate_html_report,
        generate_markdown_report,
        generate_text_report,
    )

    result = run_dfm_on_gerber_zip(
        Path(args.gerber_zip),
        ruleset_id=args.ruleset,
        design_id=args.design_id,
        design_data=args.design_data,
        bom=args.bom,
    )

    if args.format == "json":
        text = result.to_json()
    elif args.format == "markdown":
        text = generate_markdown_report(result)
    elif args.format == "html":
        geometry = build_geometry_for(Path(args.gerber_zip))
        text = generate_html_report(result, geometry)
    else:
        text = generate_text_report(result)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.format} report to {args.output}")
    else:
        print(text)
    return 0


_STATUS_ORDER = {"pass": 0, "warning": 1, "fail": 2}
_FAIL_ON_LEVEL = {"never": 99, "warning": 1, "fail": 2}


def _cmd_gate(args: argparse.Namespace) -> int:
    """CI gate: run the full DFM, write JSON / HTML / summary artifacts, print a
    PR-ready summary, and exit non-zero when the result breaches the threshold."""
    from ..engine.run import build_geometry_for, run_dfm_on_gerber_zip
    from ..report import generate_html_report, generate_pr_summary

    zip_path = Path(args.gerber_zip)
    result = run_dfm_on_gerber_zip(
        zip_path,
        ruleset_id=args.ruleset,
        design_id=args.design_id,
        design_data=args.design_data,
        bom=args.bom,
    )

    def _write(path, text):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")

    if args.json:
        _write(args.json, result.to_json())
    if args.html:
        _write(args.html, generate_html_report(result, build_geometry_for(zip_path)))
    summary = generate_pr_summary(result)
    if args.summary:
        _write(args.summary, summary)
    print(summary)

    status = result.summary.status
    failed = _STATUS_ORDER.get(status, 0) >= _FAIL_ON_LEVEL[args.fail_on]
    if args.min_score is not None and result.summary.overall_score < args.min_score:
        failed = True
    return 1 if failed else 0


def _cmd_check(args: argparse.Namespace) -> int:
    from ..checks.definitions import (
        load_check_definition,
        load_check_definitions_for_ruleset,
    )
    from ..engine.check_runner import run_single_check
    from ..ingest.design_data import load_design_data

    # Honor ruleset threshold overrides for the single check, if any.
    check_def = None
    if args.ruleset and args.ruleset != "default":
        for d in load_check_definitions_for_ruleset(args.ruleset):
            if d.id == args.check_id:
                check_def = d
                break
    if check_def is None:
        check_def = load_check_definition(args.check_id)
    # Resolve design data (+ optional BOM) up front; run_single_check passes a
    # ready DesignData through unchanged.
    design_data = load_design_data(args.design_data, bom=args.bom)
    result = run_single_check(
        gerber_zip=Path(args.gerber_zip),
        check_def=check_def,
        ruleset_id=args.ruleset,
        design_id=args.design_id,
        design_data=design_data,
    )
    print(result.to_json() if hasattr(result, "to_json") else result.model_dump_json(indent=2))
    return 0


def _cmd_list_checks(args: argparse.Namespace) -> int:
    from ..checks.definitions import (
        load_all_check_definitions,
        load_check_definitions_for_ruleset,
    )

    defs = (load_check_definitions_for_ruleset(args.ruleset)
            if args.ruleset and args.ruleset != "default"
            else load_all_check_definitions())
    for d in defs:
        print(f"{d.id:<40} [{d.category_id}] {d.name}")
    return 0


def _cmd_list_rulesets(args: argparse.Namespace) -> int:
    from ..checks.definitions import _load_ruleset_profile, list_ruleset_ids

    for rid in list_ruleset_ids():
        try:
            meta = _load_ruleset_profile(rid).get("metadata", {})
        except Exception:
            meta = {}
        name = meta.get("name", rid)
        notes = meta.get("process_notes", "")
        print(f"{rid:<22} {name}")
        if notes:
            print(f"{'':<22} {notes}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcb-dfm", description="PCB DFM engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the full DFM ruleset on a Gerber zip")
    p_run.add_argument(
        "gerber_zip",
        help="Gerber/Excellon zip, or a KiCad .kicad_pcb / project directory "
             "(plotted via kicad-cli when KiCad is installed)",
    )
    p_run.add_argument("--ruleset", default="default")
    p_run.add_argument("--design-id", default="board")
    p_run.add_argument("--design-data", default=None,
                       help="design-data source: a KiCad project dir/.kicad_pcb, a JSON sidecar, or IPC-2581 XML (stackup / nets / placement)")
    p_run.add_argument("--bom", default=None,
                       help="BOM CSV, merged onto placement by refdes (part id / DNP for assembly checks)")
    p_run.add_argument("--format", choices=["text", "markdown", "json", "html"], default="text")
    p_run.add_argument("-o", "--output", default=None)
    p_run.set_defaults(func=_cmd_run)

    p_gate = sub.add_parser("gate", help="CI gate: run, write artifacts, exit non-zero on breach")
    p_gate.add_argument("gerber_zip")
    p_gate.add_argument("--ruleset", default="default")
    p_gate.add_argument("--design-id", default="board")
    p_gate.add_argument("--design-data", default=None)
    p_gate.add_argument("--bom", default=None,
                        help="BOM CSV, merged onto placement by refdes")
    p_gate.add_argument("--fail-on", choices=["never", "warning", "fail"], default="fail",
                        help="exit non-zero when overall status reaches this level")
    p_gate.add_argument("--min-score", type=float, default=None,
                        help="also fail if overall score is below this")
    p_gate.add_argument("--html", default=None, help="write the HTML report here")
    p_gate.add_argument("--json", default=None, help="write the JSON result here")
    p_gate.add_argument("--summary", default=None, help="write the Markdown summary here")
    p_gate.set_defaults(func=_cmd_gate)

    p_check = sub.add_parser("check", help="Run a single check by id")
    p_check.add_argument("gerber_zip")
    p_check.add_argument("check_id")
    p_check.add_argument("--ruleset", default="default")
    p_check.add_argument("--design-id", default="board")
    p_check.add_argument("--design-data", default=None,
                         help="design-data source: a KiCad project dir/.kicad_pcb, a JSON sidecar, or IPC-2581 XML (stackup / nets / placement)")
    p_check.add_argument("--bom", default=None,
                         help="BOM CSV, merged onto placement by refdes")
    p_check.set_defaults(func=_cmd_check)

    p_list = sub.add_parser("list-checks", help="List available check ids")
    p_list.add_argument("--ruleset", default="default",
                        help="show the checks a ruleset selects")
    p_list.set_defaults(func=_cmd_list_checks)

    p_rs = sub.add_parser("list-rulesets", help="List available fab capability profiles")
    p_rs.set_defaults(func=_cmd_list_rulesets)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="emit timing/diagnostic logs to stderr",
    )
    args = parser.parse_args(argv)
    if getattr(args, "verbose", False):
        logging.basicConfig(
            level=logging.INFO, stream=sys.stderr, format="%(message)s"
        )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
