# pcb_dfm/engine/run.py
from pathlib import Path
from ..ingest import ingest_gerber_zip
from ..geometry import build_board_geometry
from ..checks.definitions import load_check_definitions_for_ruleset
from .context import CheckContext
from .check_runner import get_check_runner
from ..results import DfmResult


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

