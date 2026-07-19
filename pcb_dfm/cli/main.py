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
import sys
from pathlib import Path


def _cmd_run(args: argparse.Namespace) -> int:
    from ..engine.run import run_dfm_on_gerber_zip
    from ..report import generate_text_report, generate_markdown_report

    result = run_dfm_on_gerber_zip(
        Path(args.gerber_zip),
        ruleset_id=args.ruleset,
        design_id=args.design_id,
    )

    if args.format == "json":
        text = result.to_json()
    elif args.format == "markdown":
        text = generate_markdown_report(result)
    else:
        text = generate_text_report(result)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.format} report to {args.output}")
    else:
        print(text)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from ..checks.definitions import load_check_definition
    from ..engine.check_runner import run_single_check

    check_def = load_check_definition(args.check_id)
    result = run_single_check(
        gerber_zip=Path(args.gerber_zip),
        check_def=check_def,
        ruleset_id=args.ruleset,
        design_id=args.design_id,
    )
    print(result.to_json() if hasattr(result, "to_json") else result.model_dump_json(indent=2))
    return 0


def _cmd_list_checks(args: argparse.Namespace) -> int:
    from ..checks.definitions import load_all_check_definitions

    for d in load_all_check_definitions():
        print(f"{d.id:<40} [{d.category_id}] {d.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcb-dfm", description="PCB DFM engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the full DFM ruleset on a Gerber zip")
    p_run.add_argument("gerber_zip")
    p_run.add_argument("--ruleset", default="default")
    p_run.add_argument("--design-id", default="board")
    p_run.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    p_run.add_argument("-o", "--output", default=None)
    p_run.set_defaults(func=_cmd_run)

    p_check = sub.add_parser("check", help="Run a single check by id")
    p_check.add_argument("gerber_zip")
    p_check.add_argument("check_id")
    p_check.add_argument("--ruleset", default="default")
    p_check.add_argument("--design-id", default="board")
    p_check.set_defaults(func=_cmd_check)

    p_list = sub.add_parser("list-checks", help="List all available check ids")
    p_list.set_defaults(func=_cmd_list_checks)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
