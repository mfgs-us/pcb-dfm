"""
Tests for the `pcb-dfm gate` CI command: exit codes vs thresholds and the
JSON / HTML / summary artifacts it writes. This is the logic the GitHub Action
shells out to.
"""

from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

_REPO = Path(__file__).resolve().parent.parent

# These tests exercise the GATE MECHANISM, so each needs a board that actually
# has the status it is gating on. mini_board warns (score 75); pcbtools_example,
# a real 1990s-era design, genuinely fails. mini_board used to fail too, which is
# why one fixture served both -- it stopped failing once the false-positive floor
# was fixed (a clean-ish board no longer scores 0).
WARNING_BOARD = _REPO / "testdata" / "mini_board.zip"
FAILING_BOARD = _REPO / "testdata" / "pcbtools_example.zip"

pytestmark = pytest.mark.skipif(
    not (WARNING_BOARD.exists() and FAILING_BOARD.exists()),
    reason="gerber fixture missing",
)


def _gate(*args, board=None):
    from pcb_dfm.cli.main import main
    return main(["gate", str(board or WARNING_BOARD), *args])


def test_gate_fails_and_writes_artifacts(tmp_path):
    html = tmp_path / "r.html"
    js = tmp_path / "r.json"
    md = tmp_path / "s.md"
    rc = _gate("--fail-on", "fail",
               "--html", str(html), "--json", str(js), "--summary", str(md),
               board=FAILING_BOARD)
    assert rc == 1
    assert "<svg" in html.read_text()
    assert '"schema_version"' in js.read_text()
    summary = md.read_text()
    assert "PCB DFM" in summary and "FAIL" in summary


def test_gate_report_only_passes():
    assert _gate("--fail-on", "never") == 0


def test_gate_fail_on_warning():
    assert _gate("--fail-on", "warning") == 1


def test_gate_does_not_fail_a_board_that_only_warns():
    # The floor this protects: a board that merely warns must not trip
    # --fail-on fail. mini_board scores 75.
    assert _gate("--fail-on", "fail") == 0


def test_gate_min_score_gate():
    # Status is ignored (never), so only the score decides. mini_board is 75.
    assert _gate("--fail-on", "never", "--min-score", "80") == 1
    assert _gate("--fail-on", "never", "--min-score", "0") == 0
