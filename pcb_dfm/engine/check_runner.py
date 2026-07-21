from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Dict, Iterable

from ..checks import _ensure_impls_loaded
from ..checks.definitions import CheckDefinition
from ..geometry import build_board_geometry
from ..geometry.gerber_compat import rU_open_shim
from ..ingest import ingest_gerber_zip
from ..ingest.design_data import DesignDataLike, load_design_data
from ..results import CheckResult, Violation
from .context import CheckContext
from .geometry_cache import GeometryCache

logger = logging.getLogger("pcb_dfm.timing")

# Checks whose results rely on shape/role guessing rather than direct
# measurement (via-pad inference, silkscreen-on-copper via bbox overlap, plane
# splitting heuristics, ...). Their findings are labelled "heuristic" so users
# treat them as a checklist, not a hard gate. Everything else is "high".
HEURISTIC_CHECK_IDS = {
    "silkscreen_on_copper",
    "silkscreen_over_mask_defined_pads",
    "via_in_pad_thermal_balance",
    "via_tenting",
    "copper_density_balance",
    "plane_fragmentation",
    "acid_trap_angle",
    "copper_thermal_area",
    "thermal_relief_spoke_width",
    "tombstoning_risk",
    "wave_solder_shadowing",
    "solder_paste_area_coverage",
    "missing_tooling_holes",
}


def _confidence_for(check_def) -> str:
    explicit = (check_def.raw or {}).get("confidence")
    if explicit:
        return str(explicit)
    return "heuristic" if check_def.id in HEURISTIC_CHECK_IDS else "high"


def _log(msg: str) -> None:
    # Diagnostics go through the logging module (a NullHandler is installed on
    # the package logger, so nothing is emitted unless the application opts in;
    # the CLI configures a stderr handler). This keeps stdout clean for
    # machine-readable output such as the CLI emitting JSON.
    logger.info("%s", msg)

from .check_defs import CheckDefinition as EngineCheckDefinition
from .check_defs import PathLike as _PathLike
from .check_defs import load_check_definition as _load_check_definition

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
    design_data: DesignDataLike = None,
) -> CheckResult:
    """
    Run a single DFM check on a Gerber zip archive.
    """

    _ensure_impls_loaded()

    gerber_zip = Path(gerber_zip).resolve()
    design_data = load_design_data(design_data)

    # The pcb-tools "rU" open-mode shim is active only for the duration of this
    # block (ingest, geometry build, and the check run all call gerber.read).
    with rU_open_shim():
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
            design_data=design_data,
        )

        runner = get_check_runner(check_def.id)

        t1 = time.perf_counter()
        result = runner(ctx)
        t_run = time.perf_counter() - t1

    _log(
        f"[DFM TIMING] {check_def.id:<40} "
        f"setup={t_setup:6.3f}s  run={t_run:6.3f}s  total={t_setup + t_run:6.3f}s"
    )

    # Enforce the same invariants as the batch runner (severity/score
    # consistency). Previously only run_checks() finalized, so this path
    # could emit contradictions like status="pass" with severity="error".
    if isinstance(result, dict):
        result = CheckResult(**result)
    if isinstance(result, CheckResult):
        result = result.finalize()
        if result.confidence is None:
            result.confidence = _confidence_for(check_def)

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
    design_data: DesignDataLike = None,
    prebuilt_ingest=None,
) -> list[CheckResult]:
    """
    Run multiple checks in one pass over the input.

    Ingest + geometry are built once and shared. Pass ``prebuilt_ingest`` to
    reuse an already-computed ingest (e.g. when the caller needs the stackup
    inventory) and avoid ingesting twice.
    """

    _ensure_impls_loaded()

    gerber_zip = Path(gerber_zip).resolve()
    design_data = load_design_data(design_data)

    # ---- Shared ingest + geometry (major speed win). pcb-tools "rU" shim is
    # scoped to the read calls rather than patched globally at import time.
    t0 = time.perf_counter()
    with rU_open_shim():
        ingest_result = prebuilt_ingest if prebuilt_ingest is not None else ingest_gerber_zip(gerber_zip)
        geom = build_board_geometry(ingest_result)
    setup_time = time.perf_counter() - t0

    cache = GeometryCache()

    _log(f"[DFM TIMING] shared setup (ingest+geometry): {setup_time:.3f}s")

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
            design_data=design_data,
        )

        # A check with no registered implementation is genuinely
        # "not applicable" (it was never wired up) -- but it must NOT abort
        # the whole batch, which the previous unguarded get_check_runner()
        # call did via an uncaught KeyError.
        try:
            runner = get_check_runner(check_def.id)
        except KeyError:
            _log(
                f"[DFM TIMING] {check_def.id:<40} "
                f"SKIPPED: no registered implementation"
            )
            results.append(CheckResult(
                check_id=check_def.id,
                category_id=check_def.category_id,
                status="not_applicable",
                severity="info",
                score=None,
                confidence=_confidence_for(check_def),
            ).finalize())
            continue

        t1 = time.perf_counter()
        try:
            with rU_open_shim():
                result = runner(ctx)
        except Exception as exc:
            t_run = time.perf_counter() - t1
            _log(
                f"[DFM TIMING] {check_def.id:<40} "
                f"run={t_run:6.3f}s  ERROR: {exc}"
            )
            # A crash is NOT a pass. For a manufacturing gate, silently
            # returning not_applicable/100 here would hide real defects on
            # exactly the boards a check crashes on. Surface it as a failed
            # check with an error violation so it is visible and scores 0.
            results.append(CheckResult(
                check_id=check_def.id,
                category_id=check_def.category_id,
                status="fail",
                severity="error",
                score=0.0,
                confidence=_confidence_for(check_def),
                violations=[Violation(
                    message=f"Check crashed: {type(exc).__name__}: {exc}",
                    severity="error",
                )],
            ).finalize())
            continue
        t_run = time.perf_counter() - t1

        # Auto-finalize and coerce results for robustness
        if isinstance(result, dict):
            # Coerce dict to CheckResult
            result = CheckResult(**result)

        if isinstance(result, CheckResult):
            # Always finalize to enforce invariants
            result = result.finalize()
            if result.confidence is None:
                result.confidence = _confidence_for(check_def)

        _log(
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
