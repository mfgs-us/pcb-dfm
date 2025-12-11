#!/usr/bin/env python
"""
Test that running a check by id and by its JSON definition file
produce equivalent results.

This is a good guardrail for the "custom checks by file" mechanism.

Usage:

    py -3 scripts/test_custom_check_equivalence.py Gerbers.zip
"""

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

from importlib.resources import files

from pcb_dfm.engine.check_defs import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check


def result_to_comparable_dict(result) -> Dict[str, Any]:
    """
    Normalize a CheckResult to a plain dict with only the fields
    that should match between id based and file based runs.
    """
    return {
        "check_id": result.check_id,
        "name": getattr(result, "name", ""),
        "status": result.status,
        "severity": result.severity,
        "score": result.score,
        "metric": {
            "kind": result.metric.kind,
            "units": result.metric.units,
            "measured_value": result.metric.measured_value,
            "target": result.metric.target,
            "limit_low": result.metric.limit_low,
            "limit_high": result.metric.limit_high,
        },
        "violations_count": len(result.violations),
        # You could add more fields here if you want stronger equality
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare check run by id vs by JSON definition file."
    )
    parser.add_argument(
        "gerber_zip",
        type=str,
        help="Path to the Gerbers zip file (e.g. Gerbers.zip)",
    )
    parser.add_argument(
        "--check-id",
        type=str,
        default="plane_fragmentation",
        help="Check id to test (default: plane_fragmentation)",
    )

    args = parser.parse_args()

    gerber_zip = Path(args.gerber_zip).resolve()
    if not gerber_zip.exists():
        raise SystemExit(f"Gerber zip not found: {gerber_zip}")

    check_id = args.check_id

    # Resolve the JSON file inside installed package data
    checks_dir = Path(files("pcb_dfm").joinpath("check_data", "checks"))
    json_path = checks_dir.joinpath(f"{check_id}.json")

    if not json_path.exists():
        raise SystemExit(f"Check JSON not found: {json_path}")

    print(f"Gerbers : {gerber_zip}")
    print(f"Check id: {check_id}")
    print(f"JSON    : {json_path}")

    # Run via id
    check_def_id = load_check_definition(check_id)
    res_id = run_single_check(
        gerber_zip=gerber_zip,
        check_def=check_def_id,
        ruleset_id="test_ruleset",
        design_id="test_design_id",
    )

    # Run via explicit JSON path
    check_def_file = load_check_definition(json_path)
    res_file = run_single_check(
        gerber_zip=gerber_zip,
        check_def=check_def_file,
        ruleset_id="test_ruleset",
        design_id="test_design_id",
    )

    dict_id = result_to_comparable_dict(res_id)
    dict_file = result_to_comparable_dict(res_file)

    print("")
    print("Result from id:   ", dict_id)
    print("Result from file: ", dict_file)
    print("")

    if dict_id == dict_file:
        print("OK - id based and file based results are equivalent.")
        return 0
    else:
        print("FAIL - id based and file based results differ.")
        # Optionally be stricter: nonzero exit code so CI can catch it
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
