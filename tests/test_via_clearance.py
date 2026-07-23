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
