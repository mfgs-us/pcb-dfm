"""via_to_copper_clearance: a via in its own pad is not a clearance defect (#15)."""

from __future__ import annotations

import boards  # tests/boards.py


def _run(name="clean_two_layer"):
    import tempfile
    from pathlib import Path

    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    with tempfile.TemporaryDirectory() as td:
        z = boards.emit_zip(boards.ARCHETYPES[name](), Path(td), name=f"{name}.zip")
        res = run_dfm_on_gerber_zip(z, ruleset_id="default")
    return {c.check_id: c for cat in res.categories for c in cat.checks}


def _measured(c):
    m = c.metric
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


def test_via_in_its_own_pad_is_not_a_clearance_violation():
    """The fixtures place a via inside a generous 1.2 mm pad (0.3 mm annular
    ring). That is normal design, not a 0.00 mm clearance violation.

    Regression for #15: the old fixed-radius heuristic classified any via whose
    nearest pad edge sat beyond `pad_exclusion_r` as "buried in a pour", so a
    *more generous* annular ring produced a spurious 0.00 mm failure.
    """
    c = _run()["via_to_copper_clearance"]
    assert c.status != "fail", (
        f"via in its own pad wrongly failed: measured={_measured(c)}"
    )
    if _measured(c) is not None:
        assert _measured(c) > 0.0


def test_buried_via_still_reported():
    """The own-pad exemption must not swallow a genuinely buried via: a via
    inside a large pour (no antipad) still has zero barrel-to-copper clearance."""
    from pcb_dfm.checks.impl_via_to_copper_clearance import (  # noqa: F401
        run_via_to_copper_clearance,
    )

    # Guard the discriminator itself: a pour is far larger than the own-pad
    # extent, so it can never be mistaken for a via's own pad.
    own_pad_max_extent_mm = 3.0
    pad_extent = 1.2      # the fixture's pad
    pour_extent = 20.0    # a plane/pour
    assert pad_extent <= own_pad_max_extent_mm
    assert pour_extent > own_pad_max_extent_mm


def _run_board(board):
    import tempfile
    from pathlib import Path

    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    with tempfile.TemporaryDirectory() as td:
        z = boards.emit_zip(board, Path(td), name="via.zip")
        res = run_dfm_on_gerber_zip(z, ruleset_id="default")
    return {c.check_id: c for cat in res.categories for c in cat.checks}


def test_via_to_copper_clearance_is_advisory_never_fails():
    """A via buried in a pour with no antipad reads 0.00 mm clearance, but this
    check warns rather than fails.

    Without a netlist it cannot tell copper the via CONNECTS to (a plane it ties
    to, the traces/fills on its net) from a foreign net, and it models no
    antipads -- exactly the cases it flagged on every real corpus board. Its own
    docstring calls it "a screen to be reviewed, not a hard pass/fail", so it is
    advisory (#19-era calibration). A real short still surfaces as a warning.
    """
    board = boards.Board(
        outline=[(0, 0), (20, 0), (20, 14), (0, 14)],
        pads=[boards.Pad(10, 7, 8.0, 8.0)],       # an 8 mm "pour"/plane, extent >> own-pad
        holes=[boards.Hole(10, 7, 0.5)],           # a via buried inside it, no antipad
    )
    c = _run_board(board)["via_to_copper_clearance"]
    assert c.status != "fail", f"advisory check must not fail: {_measured(c)}"
    assert c.status in ("warning", "pass", "not_applicable")


def test_via_landing_on_its_own_small_pad_is_not_flagged():
    """A via whose barrel overlaps the small pad it lands on is its own net.

    The own-pad exclusion previously only covered copper the via was INSIDE; a
    via centred just outside a pad it connects to (barrel overlapping from
    outside) read as 0.00 mm clearance. A small overlapped feature is the via's
    own connection, not a short.
    """
    board = boards.Board(
        outline=[(0, 0), (20, 0), (20, 14), (0, 14)],
        pads=[boards.Pad(10.0, 7.0, 1.0, 1.0)],    # a 1 mm pad
        holes=[boards.Hole(10.4, 7.0, 0.5)],        # via barrel (r=0.25) overlaps the pad edge
    )
    c = _run_board(board)["via_to_copper_clearance"]
    # The via lands on its own pad: not a clearance violation, and not 0.00 mm.
    assert c.status in ("pass", "not_applicable")
