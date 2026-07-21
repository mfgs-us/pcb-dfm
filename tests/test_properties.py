"""
Property-based geometric invariants. Instead of one hand-picked input, these
assert relationships that must hold for ANY board, catching whole classes of
geometry bugs (unit errors, non-invariant math, nondeterminism):

  - scale by k  -> distance/size metrics scale by k
  - translate   -> measured values unchanged
  - mirror      -> measured values + counts unchanged
  - determinism -> identical result across runs
"""

from pathlib import Path

import boards  # tests/boards.py
import pytest

pytest.importorskip("gerber", reason="pcb-tools (gerber) not installed")


def _min_trace_width(zpath: Path):
    from pcb_dfm.checks.definitions import load_check_definition
    from pcb_dfm.engine.check_runner import run_single_check
    return run_single_check(zpath, load_check_definition("min_trace_width")).metric.measured_value


BASE_W = 0.30  # clean_two_layer trace width


@pytest.mark.parametrize("k", [0.5, 2.0, 3.0])
def test_scale_invariance(tmp_path, k):
    z = boards.emit_zip(boards.clean_two_layer(), tmp_path,
                        transform=lambda x, y: (x * k, y * k), wscale=k, name=f"s{k}.zip")
    assert _min_trace_width(z) == pytest.approx(BASE_W * k, rel=1e-3)


@pytest.mark.parametrize("dx,dy", [(5.0, 5.0), (100.0, 0.0), (0.0, 37.5)])
def test_translation_invariance(tmp_path, dx, dy):
    z = boards.emit_zip(boards.clean_two_layer(), tmp_path,
                        transform=lambda x, y: (x + dx, y + dy), name=f"t{dx}_{dy}.zip")
    assert _min_trace_width(z) == pytest.approx(BASE_W, rel=1e-3)


def test_mirror_invariance(tmp_path):
    # Mirror across the board width (outline is 20 mm wide).
    z = boards.emit_zip(boards.clean_two_layer(), tmp_path,
                        transform=lambda x, y: (20.0 - x, y), name="mirror.zip")
    assert _min_trace_width(z) == pytest.approx(BASE_W, rel=1e-3)


def test_determinism(tmp_path):
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip
    z = boards.emit_zip(boards.clean_two_layer(), tmp_path, name="det.zip")
    d1 = boards.result_digest(run_dfm_on_gerber_zip(z, ruleset_id="default"))
    d2 = boards.result_digest(run_dfm_on_gerber_zip(z, ruleset_id="default"))
    assert d1 == d2
