from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Iterable
import time

from ..checks import _ensure_impls_loaded
from ..checks.definitions import CheckDefinition
from ..ingest import ingest_gerber_zip
from ..geometry import build_board_geometry
from ..results import CheckResult
from .context import CheckContext
from .geometry_cache import GeometryCache

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

    _ensure_impls_loaded()

    gerber_zip = Path(gerber_zip).resolve()

    t0 = time.perf_counter()
    ingest_result = ingest_gerber_zip(gerber_zip)
    geom = build_board_geometry(ingest_result)
    t_setup = time.perf_counter() - t0

    cache = GeometryCache()

    ctx = CheckContext(
        check_def=check_def,
        ingest=ingest_result,
        geometry=geom,
        geometry_cache=cache,
        ruleset_id=ruleset_id,
        design_id=design_id,
        gerber_zip=gerber_zip,
    )

    runner = get_check_runner(check_def.id)

    t1 = time.perf_counter()
    result = runner(ctx)
    t_run = time.perf_counter() - t1

    print(
        f"[DFM TIMING] {check_def.id:<40} "
        f"setup={t_setup:6.3f}s  run={t_run:6.3f}s  total={t_setup + t_run:6.3f}s"
    )

    return result


def run_single_check_from_definition_file(
    gerber_zip: Path | str,
    definition: _PathLike,
    ruleset_id: str = "custom",
    design_id: str = "board",
) -> CheckResult:
    """
    Convenience helper.
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

    Ingest + geometry are built once and shared.
    """

    _ensure_impls_loaded()

    gerber_zip = Path(gerber_zip).resolve()

    # ---- Shared ingest + geometry (major speed win)
    t0 = time.perf_counter()
    ingest_result = ingest_gerber_zip(gerber_zip)
    geom = build_board_geometry(ingest_result)
    setup_time = time.perf_counter() - t0

    cache = GeometryCache()

    print(f"[DFM TIMING] shared setup (ingest+geometry): {setup_time:.3f}s")

    results: list[CheckResult] = []

    for check_def in check_defs:
        ctx = CheckContext(
            check_def=check_def,
            ingest=ingest_result,
            geometry=geom,
            geometry_cache=cache,
            ruleset_id=ruleset_id,
            design_id=design_id,
            gerber_zip=gerber_zip,
        )

        runner = get_check_runner(check_def.id)

        t1 = time.perf_counter()
        result = runner(ctx)
        t_run = time.perf_counter() - t1

        # Auto-finalize and coerce results for robustness
        if isinstance(result, dict):
            # Coerce dict to CheckResult
            result = CheckResult(**result)
        
        if isinstance(result, CheckResult):
            # Always finalize to enforce invariants
            result = result.finalize()

        print(
            f"[DFM TIMING] {check_def.id:<40} "
            f"run={t_run:6.3f}s"
        )

        results.append(result)

    return results


def run_checks_from_definition_files(
    gerber_zip: Path | str,
    definitions: Iterable[_PathLike],
    ruleset_id: str = "custom",
    design_id: str = "board",
) -> list[CheckResult]:
    """
    Higher level helper that accepts a list of JSON definition paths or ids.
    """
    check_defs = [_load_check_definition(d) for d in definitions]
    return run_checks(
        gerber_zip=gerber_zip,
        check_defs=check_defs,
        ruleset_id=ruleset_id,
        design_id=design_id,
    )
