# pcb_dfm/engine/check_runner.py

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict

from ..checks import _ensure_impls_loaded

from ..checks.definitions import CheckDefinition
from ..ingest import ingest_gerber_zip
from ..geometry import build_board_geometry
from ..results import CheckResult  # uses your existing results.py
from .context import CheckContext

from typing import Iterable
from .check_defs import load_check_definition as _load_check_definition
from .check_defs import CheckDefinition as EngineCheckDefinition, PathLike as _PathLike

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
    gerber_zip: Path | str,
    check_def: CheckDefinition,
    ruleset_id: str = "default",
    design_id: str = "board",
) -> CheckResult:
    """
    Run a single DFM check on a Gerber zip archive.
    """

    # Make sure all impl_* modules are imported and their runners are registered
    _ensure_impls_loaded()

    gerber_zip = Path(gerber_zip).resolve()

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



def run_single_check_from_definition_file(
    gerber_zip: Path | str,
    definition: _PathLike,
    ruleset_id: str = "custom",
    design_id: str = "board",
) -> CheckResult:
    """
    Convenience helper:

    - Accepts a check definition as either:
      - a path to a JSON file, or
      - a check id string that matches one of the built in definitions.
    - Loads the CheckDefinition and runs it against the given Gerber zip.
    """
    check_def = _load_check_definition(definition)
    return run_single_check(
        gerber_zip=gerber_zip,
        check_def=check_def,
        ruleset_id=ruleset_id,
        design_id=design_id,
    )


def run_checks(
    gerber_zip: Path | str,
    check_defs: Iterable[EngineCheckDefinition],
    ruleset_id: str = "custom",
    design_id: str = "board",
) -> list[CheckResult]:
    """
    Run multiple checks in one pass over the input.

    This is similar to engine.run.run_dfm_on_gerber_zip, but instead of
    looking up checks by ruleset id, you pass in concrete CheckDefinition
    objects (which can include custom ones).
    """
    # Make sure built in impl_* modules have registered their runners
    _ensure_impls_loaded()

    gerber_zip = Path(gerber_zip).resolve()

    ingest_result = ingest_gerber_zip(gerber_zip)
    geom = build_board_geometry(ingest_result)

    results: list[CheckResult] = []
    for check_def in check_defs:
        ctx = CheckContext(
            check_def=check_def,
            ingest=ingest_result,
            geometry=geom,
            ruleset_id=ruleset_id,
            design_id=design_id,
            gerber_zip=gerber_zip,
        )
        runner = get_check_runner(check_def.id)
        results.append(runner(ctx))

    return results


def run_checks_from_definition_files(
    gerber_zip: Path | str,
    definitions: Iterable[_PathLike],
    ruleset_id: str = "custom",
    design_id: str = "board",
) -> list[CheckResult]:
    """
    Higher level helper that accepts a list of JSON definition paths or ids.

    Example:
        run_checks_from_definition_files(
            \"Gerbers.zip\",
            [\"min_trace_width\", Path(\"./my_custom_check.json\")],
        )
    """
    check_defs = [ _load_check_definition(d) for d in definitions ]
    return run_checks(
        gerber_zip=gerber_zip,
        check_defs=check_defs,
        ruleset_id=ruleset_id,
        design_id=design_id,
    )