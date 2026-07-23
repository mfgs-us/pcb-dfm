"""
Tests for the `pcb-dfm gate` CI command: exit codes vs thresholds and the
JSON / HTML / summary artifacts it writes. This is the logic the GitHub Action
shells out to.
"""

from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

_REPO = Path(__file__).resolve().parent.parent
GERBER = _REPO / "testdata" / "mini_board.zip"

pytestmark = pytest.mark.skipif(not GERBER.exists(), reason="gerber fixture missing")


def _gate(*args):
    from pcb_dfm.cli.main import main
    return main(["gate", str(GERBER), *args])


def test_gate_fails_and_writes_artifacts(tmp_path):
    html = tmp_path / "r.html"
    js = tmp_path / "r.json"
    md = tmp_path / "s.md"
    rc = _gate("--fail-on", "fail",
               "--html", str(html), "--json", str(js), "--summary", str(md))
    # mini_board fails DFM, --fail-on fail -> non-zero exit.
    assert rc == 1
    assert "<svg" in html.read_text()
    assert '"schema_version"' in js.read_text()
    summary = md.read_text()
    assert "PCB DFM" in summary and "FAIL" in summary


def test_gate_report_only_passes():
    assert _gate("--fail-on", "never") == 0


def test_gate_fail_on_warning():
    assert _gate("--fail-on", "warning") == 1


def test_gate_min_score_gate():
    # Status is ignored (never), but the low score trips the min-score gate.
    assert _gate("--fail-on", "never", "--min-score", "50") == 1
    assert _gate("--fail-on", "never", "--min-score", "0") == 0
