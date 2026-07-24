"""The engine must never hang or crash on malformed input.

A fab-facing tool receives whatever a user uploads: truncated files, empty
archives, artwork that is not Gerber at all, and -- the one that actually bit --
Gerbers carrying absurd coordinates. A file with a ``999999999999`` coordinate
made several checks allocate a board-extent-sized spatial grid (tens of billions
of cells) and hang the run indefinitely.

This suite runs the whole engine over a battery of bad inputs under a hard time
limit. It is the real safety net: it catches any gridding check that forgets the
extent guard, now or in future, without anyone having to remember which checks
are vulnerable.
"""

from __future__ import annotations

import signal
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.engine.run import run_dfm_on_gerber_zip  # noqa: E402

_HUGE = "999999999999"  # ~10^6 mm in 4.6 format: no real board, sure to explode a grid


_CASES = {
    "empty_zip": {},
    "not_gerber": {"a.gtl": "this is not gerber at all\n"},
    "truncated_gerber": {"board.gtl": "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.2*%\nD10*\nX1000000Y100"},
    "empty_files": {"board.gtl": "", "board.drl": ""},
    "only_a_drill": {"board.drl": "M48\nMETRIC,TZ\nT1C0.3\n%\nT1\nX5Y5\nT0\nM30\n"},
    "huge_coordinates": {
        "board.gtl": f"%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.2*%\nD10*\n"
                     f"X{_HUGE}Y{_HUGE}D02*\nX1Y1D01*\nM02*\n"
    },
    "huge_copper_with_drills": {
        "board.gtl": f"%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.2*%\nD10*\n"
                     f"X{_HUGE}Y1D02*\nX1Y1D01*\nM02*\n",
        "board.drl": "M48\nMETRIC,TZ\nT1C0.3\n%\nT1\nX5Y5\nX6Y5\nT0\nM30\n",
    },
    "negative_huge": {
        "board.gtl": f"%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.2*%\nD10*\n"
                     f"X-{_HUGE}Y1D02*\nX1Y1D01*\nM02*\n"
    },
}


class _Timeout(Exception):
    pass


def _run_bounded(zip_path: Path, seconds: int = 30):
    """Run the engine, raising _Timeout if it does not finish in time.

    A hang is the failure mode under test, and a wall clock via SIGALRM catches
    it even when the stall is in a tight loop that never checks a deadline.
    """
    def handler(signum, frame):
        raise _Timeout()

    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        return run_dfm_on_gerber_zip(zip_path, ruleset_id="default")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_malformed_input_never_hangs_or_crashes(tmp_path, name):
    z = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for fn, content in _CASES[name].items():
            zf.writestr(fn, content)

    try:
        result = _run_bounded(z)
    except _Timeout:
        pytest.fail(f"{name}: engine hung (>30s) on malformed input")

    # It must produce a well-formed result, not raise, and every check must have
    # resolved to a real status rather than a bare None slipping through.
    assert result is not None
    statuses = [
        c.status for cat in result.categories for c in cat.checks
    ]
    assert statuses, f"{name}: no checks ran"
    assert all(
        s in ("pass", "warning", "fail", "not_applicable") for s in statuses
    ), f"{name}: a check produced an invalid status"


def test_absurd_extent_makes_gridding_checks_decline(tmp_path):
    """The specific fix: a board claiming to be ~10^6 mm wide is corrupt, and the
    geometry-gridding checks must decline (not_applicable), not measure it."""
    z = tmp_path / "huge.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("board.gtl", _CASES["huge_copper_with_drills"]["board.gtl"])
        zf.writestr("board.drl", _CASES["huge_copper_with_drills"]["board.drl"])

    result = _run_bounded(z)
    by_id = {c.check_id: c for cat in result.categories for c in cat.checks}

    for gridding in ("copper_density_balance", "via_to_copper_clearance",
                     "min_annular_ring", "plane_fragmentation", "silkscreen_on_copper"):
        assert by_id[gridding].status == "not_applicable", (
            f"{gridding} should decline on implausibly large geometry"
        )


def test_real_boards_are_not_affected_by_the_extent_guard():
    """The guard's threshold (2 m) is far above any real board, so it must never
    fire on the corpus -- otherwise it would silently disable real checks."""
    from pcb_dfm.engine.run import build_geometry_for
    from pcb_dfm.geometry.queries import geometry_extent_plausible

    for name in ("pcbtools_full", "eagle_gyw", "diptrace_fd1", "mini_board"):
        board = Path(__file__).resolve().parent.parent / "testdata" / f"{name}.zip"
        if not board.exists():
            continue
        assert geometry_extent_plausible(build_geometry_for(board)), (
            f"{name}: the extent guard must not fire on a real board"
        )
