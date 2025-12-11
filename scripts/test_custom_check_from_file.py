#!/usr/bin/env python
"""
Small integration test for pcb_dfm custom check definitions.

Usage examples:

    # Use a built in check by id
    py -3 scripts/test_custom_check_from_file.py Gerbers.zip plane_fragmentation

    # Use a custom JSON definition file
    py -3 scripts/test_custom_check_from_file.py Gerbers.zip path/to/my_custom_check.json
"""

import argparse
from pathlib import Path

from pcb_dfm.engine.check_defs import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a single DFM check using a definition id or JSON file."
    )
    parser.add_argument(
        "gerber_zip",
        type=str,
        help="Path to the Gerbers zip file (e.g. Gerbers.zip)",
    )
    parser.add_argument(
        "definition",
        type=str,
        help=(
            "Check definition to use. "
            "Either a built in check id (e.g. 'plane_fragmentation') "
            "or a path to a JSON definition file."
        ),
    )
    parser.add_argument(
        "--ruleset-id",
        type=str,
        default="custom",
        help="Ruleset id to tag the result with (default: custom)",
    )
    parser.add_argument(
        "--design-id",
        type=str,
        default="board",
        help="Design id to tag the result with (default: board)",
    )

    args = parser.parse_args()

    gerber_zip = Path(args.gerber_zip).resolve()
    if not gerber_zip.exists():
        raise SystemExit(f"Gerber zip not found: {gerber_zip}")

    definition_arg = args.definition

    # Important: load_check_definition treats str as an id, and Path as a file path.
    # So we must wrap real filesystem paths in Path() so they are handled correctly.
    def_path = Path(definition_arg)
    if def_path.exists():
        check_def = load_check_definition(def_path)
        mode = "file"
    else:
        check_def = load_check_definition(definition_arg)
        mode = "id"

    print(f"Using definition ({mode}): {check_def.id!r}")
    print(f"Running on Gerbers: {gerber_zip}")

    result = run_single_check(
        gerber_zip=gerber_zip,
        check_def=check_def,
        ruleset_id=args.ruleset_id,
        design_id=args.design_id,
    )

    # We do not assume a particular serialization API, just access attributes.
    print("")
    print("=== Check result summary ===")
    print(f"check_id : {result.check_id}")
    print(f"name     : {getattr(result, 'name', '')}")
    print(f"status   : {result.status}")
    print(f"severity : {result.severity}")
    print(f"score    : {result.score}")
    print(f"metric   : {result.metric.kind} = {result.metric.measured_value} {result.metric.units or ''}")
    print(f"violations: {len(result.violations)}")

    # Optionally print first violation for sanity
    if result.violations:
        v0 = result.violations[0]
        print("")
        print("First violation:")
        print(f"  message : {v0.message}")
        if v0.location is not None:
            print(
                f"  location: {v0.location.layer} @ "
                f"({v0.location.x_mm:.3f} mm, {v0.location.y_mm:.3f} mm)"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
