"""
Net-aware geometry check: diff_pair_spacing consistency, driven from a JSON
sidecar that carries per-net routing segments (the same geometry an IPC-2581
import provides). Exercises the net-tagged-geometry path end to end.
"""

from pathlib import Path

import pytest

pytest.importorskip("gerber", reason="pcb-tools (gerber) not installed")

_REPO = Path(__file__).resolve().parent.parent
GERBER = _REPO / "testdata" / "mini_board.zip"  # geometry irrelevant here

pytestmark = pytest.mark.skipif(not GERBER.exists(), reason="gerber fixture missing")


def _run(design_data):
    from pcb_dfm.checks.definitions import load_check_definition
    from pcb_dfm.engine.check_runner import run_single_check
    return run_single_check(GERBER, load_check_definition("diff_pair_spacing"),
                            design_data=design_data)


def _pair(seg_p, seg_n):
    """Build a sidecar with one diff pair whose two nets have the given
    segments (each seg is [[x0,y0],[x1,y1]])."""
    return {
        "nets": {
            "D_P": {"segments": seg_p, "layer": "TOP"},
            "D_N": {"segments": seg_n, "layer": "TOP"},
        },
        "diff_pairs": [{"name": "D", "positive": "D_P", "negative": "D_N"}],
    }


def test_parallel_pair_constant_gap_passes():
    # Both traces run straight with a constant 0.20 mm centre-to-centre gap.
    dd = _pair(
        seg_p=[[[0, 0], [20, 0]]],
        seg_n=[[[0, 0.2], [20, 0.2]]],
    )
    r = _run(dd)
    assert r.status == "pass"
    assert r.metric.measured_value == pytest.approx(0.0, abs=1e-6)


def test_diverging_pair_fails():
    # N diverges from 0.2 mm to 0.5 mm gap -> ~300 um variation -> fail (>50 um).
    dd = _pair(
        seg_p=[[[0, 0], [20, 0]]],
        seg_n=[[[0, 0.2], [20, 0.5]]],
    )
    r = _run(dd)
    assert r.status == "fail"
    assert r.metric.measured_value == pytest.approx(300.0, abs=1.0)


def test_not_applicable_without_geometry():
    # Diff pair declared but nets carry only length, no segments.
    dd = {
        "nets": {"D_P": {"routed_length_mm": 20.0}, "D_N": {"routed_length_mm": 20.0}},
        "diff_pairs": [{"name": "D", "positive": "D_P", "negative": "D_N"}],
    }
    assert _run(dd).status == "not_applicable"


def test_not_applicable_without_design_data():
    assert _run(None).status == "not_applicable"


def test_diff_pair_spacing_from_ipc2581_fixture():
    # The committed IPC-2581 fixture has straight parallel-ish CLK traces at a
    # constant 1 mm gap -> low variation -> not a failure.
    r = _run(_REPO / "testdata" / "sample_design.xml")
    assert r.status in ("pass", "warning")
    assert r.metric.measured_value is not None
