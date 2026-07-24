"""Verify the checks that never fire on a corpus board.

Seventeen checks report `not_applicable` on every board in the trust corpus,
because none of those boards has the feature they measure (a slot, a mousebite
row, a stackup). Thirteen at least have synthetic tests elsewhere. These four
had none, so nothing in the project proved they were correct -- a check can ship
in the default ruleset and return nonsense, or nothing, and no test would
notice:

  * ``tab_routing_mousebites`` -- real logic, never exercised by a test
  * ``backdrill_stub_length``   -- a deliberate not_applicable stub
  * ``drill_wander_budget``     -- a deliberate not_applicable stub
  * ``thermal_relief_spoke_width`` -- a deliberate not_applicable stub

The stubs are correct behaviour, not gaps: each refuses to fabricate a
measurement it cannot make from bare artwork. But "honestly declines" is a
contract worth pinning, so a future edit cannot silently turn one into a
value-emitter without a test failing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import boards  # tests/boards.py
import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.engine.run import run_dfm_on_gerber_zip  # noqa: E402


def _run(holes):
    board = boards.Board(outline=[(0, 0), (20, 0), (20, 10), (0, 10)], holes=holes)
    with tempfile.TemporaryDirectory() as td:
        z = boards.emit_zip(board, Path(td), name="b.zip")
        res = run_dfm_on_gerber_zip(z, ruleset_id="default")
    return {c.check_id: c for cat in res.categories for c in cat.checks}


def _measured(c):
    m = c.metric
    if m is None:
        return None
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


def _mousebite_row(pitch, dia=0.5, n=5, y=5.0):
    """A row of n perforation holes on the given pitch.

    Web = pitch - dia, and the check grades the worst percentage deviation of
    hole diameter and web from the ~0.5 mm recommendation, so with dia=0.5 the
    deviation is exactly |(pitch - 0.5) - 0.5| / 0.5.
    """
    return [boards.Hole(2.0 + i * pitch, y, dia) for i in range(n)]


# --------------------------------------------------------------------------
# tab_routing_mousebites -- the one with real logic
#
# Grading (limit_max = 20%): pass <= 10, warning <= 20, fail > 20.
# --------------------------------------------------------------------------
def test_a_correct_mousebite_row_passes_at_zero_deviation():
    # dia 0.5, pitch 1.0 -> web 0.5 -> exactly the recommendation.
    c = _run(_mousebite_row(pitch=1.0))["tab_routing_mousebites"]
    assert c.status == "pass"
    assert _measured(c) == pytest.approx(0.0, abs=1e-6)


def test_a_slightly_tight_web_warns():
    # pitch 0.925 -> web 0.425 -> |0.425 - 0.5| / 0.5 = 15%.
    c = _run(_mousebite_row(pitch=0.925))["tab_routing_mousebites"]
    assert c.status == "warning"
    assert _measured(c) == pytest.approx(15.0, abs=0.1)


def test_a_thin_web_fails():
    # pitch 0.8 -> web 0.3 -> |0.3 - 0.5| / 0.5 = 40%, past the 20% limit.
    c = _run(_mousebite_row(pitch=0.8))["tab_routing_mousebites"]
    assert c.status == "fail"
    assert _measured(c) == pytest.approx(40.0, abs=0.1)


def test_oversized_holes_drive_the_deviation():
    # dia 0.7 on pitch 1.2 -> web 0.5 (fine), but the hole is |0.7-0.5|/0.5 = 40%
    # over -- the check grades the WORST of diameter and web, so this fails on
    # the hole size even though the web is perfect.
    c = _run(_mousebite_row(pitch=1.2, dia=0.7))["tab_routing_mousebites"]
    assert c.status == "fail"
    assert _measured(c) == pytest.approx(40.0, abs=0.1)


def test_too_few_holes_is_not_a_mousebite_row():
    c = _run(_mousebite_row(pitch=1.0, n=2))["tab_routing_mousebites"]
    assert c.status == "not_applicable"
    assert _measured(c) is None


def test_a_two_dimensional_hole_grid_is_not_a_row():
    """A 3x3 blob of small holes is not a perforation row and must not be graded
    as one -- the collinearity guard is what stops a via field reading as a tab.
    """
    grid = [boards.Hole(2 + i * 1.0, 5 + j * 1.0, 0.5) for i in range(3) for j in range(3)]
    c = _run(grid)["tab_routing_mousebites"]
    assert c.status == "not_applicable"


def test_small_holes_spaced_too_far_apart_are_not_a_row():
    # Pitch 3.0 mm exceeds the max mousebite pitch: these are ordinary drills in
    # a line, not a perforation.
    c = _run(_mousebite_row(pitch=3.0))["tab_routing_mousebites"]
    assert c.status == "not_applicable"


def test_large_mounting_holes_are_not_mousebites():
    c = _run([boards.Hole(2 + i * 3.0, 5, 2.0) for i in range(5)])["tab_routing_mousebites"]
    assert c.status == "not_applicable"


# --------------------------------------------------------------------------
# The honest not_applicable stubs
#
# Each measures a quantity that bare artwork cannot supply (backdrill depth,
# fab registration tolerance, thermal-relief connectivity). Returning
# not_applicable is correct; fabricating a number would not be. Pin the
# contract so that stays true.
# --------------------------------------------------------------------------
_STUBS = [
    "backdrill_stub_length",
    "drill_wander_budget",
    "thermal_relief_spoke_width",
]


@pytest.mark.parametrize("check_id", _STUBS)
def test_stub_declines_without_fabricating_a_measurement(check_id):
    # A board with ordinary drills -- nothing that would let these compute a
    # real value even in principle.
    c = _run([boards.Hole(5, 5, 0.6), boards.Hole(15, 5, 0.6)])[check_id]
    assert c.status == "not_applicable", (
        f"{check_id} must decline, not grade, when its input is absent"
    )
    # The critical property: it reports NO measured value. A not_applicable with
    # a fabricated number is exactly the "warning about a quantity it never
    # measured" these were rewritten to stop doing.
    assert _measured(c) is None
    # And it explains itself rather than failing silently.
    assert c.violations and c.violations[0].message


@pytest.mark.parametrize("check_id", _STUBS)
def test_stub_runs_on_a_bare_board_without_crashing(check_id):
    """No drills at all -- the stub must still resolve, not raise."""
    board = boards.Board(outline=[(0, 0), (10, 0), (10, 10), (0, 10)])
    with tempfile.TemporaryDirectory() as td:
        z = boards.emit_zip(board, Path(td), name="b.zip")
        res = run_dfm_on_gerber_zip(z, ruleset_id="default")
    c = {c.check_id: c for cat in res.categories for c in cat.checks}[check_id]
    assert c.status == "not_applicable"
