# pcb_dfm/engine/check_runner.py

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict

from ..checks.definitions import CheckDefinition
from ..ingest import ingest_gerber_zip
from ..geometry import build_board_geometry
from ..results import CheckResult  # uses your existing results.py
from .context import CheckContext


CheckFn = Callable[[CheckContext], CheckResult]

_REGISTRY: Dict[str, CheckFn] = {}


def register_check(check_id: str) -> Callable[[CheckFn], CheckFn]:
    """
    Decorator used by individual check implementations to register
    their runner.
    """
    def decorator(fn: CheckFn) -> CheckFn:
        _REGISTRY[check_id] = fn
        return fn
    return decorator


def get_check_runner(check_id: str) -> CheckFn:
    try:
        return _REGISTRY[check_id]
    except KeyError:
        raise KeyError(f"No runner registered for check id: {check_id!r}")


def run_single_check(
    gerber_zip: Path,
    check_def: CheckDefinition,
    ruleset_id: str = "default",
    design_id: str = "board",
) -> CheckResult:
    """
    Convenience function to run a single check on a Gerber.zip.

    This will:
      - ingest Gerbers
      - build geometry
      - build a CheckContext
      - dispatch to the registered check function
    """
    gerber_zip = gerber_zip.resolve()
    ingest_result = ingest_gerber_zip(gerber_zip)
    geom = build_board_geometry(ingest_result)

    ctx = CheckContext(
        check_def=check_def,
        ingest=ingest_result,
        geometry=geom,
        ruleset_id=ruleset_id,
        design_id=design_id,
        gerber_zip=gerber_zip,
    )

    runner = get_check_runner(check_def.id)
    return runner(ctx)
