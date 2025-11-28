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
    ingest = ingest_gerber_zip(gerber_zip)
    geom = build_board_geometry(ingest)

    check_defs = load_check_definitions_for_ruleset(ruleset_id)
    check_results = []

    for check_def in check_defs:
        runner = get_check_runner(check_def.id)
        ctx = CheckContext(
            check_def=check_def,
            ingest=ingest,
            geometry=geom,
            ruleset_id=ruleset_id,
            design_id=design_id,
        )
        result = runner(ctx)
        check_results.append(result)

    # Then aggregate into DfmResult (you already have scoring scaffolding in results.py)
    dfm_result = aggregate_check_results(check_results, ruleset_id, design_id)
    return dfm_result
