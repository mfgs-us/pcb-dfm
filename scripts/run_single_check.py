from __future__ import annotations

from pathlib import Path
import sys
import json

from pcb_dfm.checks import load_all_check_definitions
from pcb_dfm.engine import run_single_check


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: py -3 scripts/run_single_check.py <Gerber.zip> <check_id>")
        raise SystemExit(1)

    gerber_zip = Path(sys.argv[1])
    check_id = sys.argv[2]

    check_defs = load_all_check_definitions()
    check_def = next((cd for cd in check_defs if cd.id == check_id), None)
    if check_def is None:
        print(f"Check id not found in checks/: {check_id}")
        raise SystemExit(1)

    result = run_single_check(gerber_zip, check_def)

    # Print a compact JSON representation for debugging
    try:
        print(json.dumps(result.model_dump(), indent=2))
    except AttributeError:
        # If CheckResult is not pydantic, just repr it
        print(result)


if __name__ == "__main__":
    main()
