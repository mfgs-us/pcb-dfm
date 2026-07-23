"""
Tests for the self-contained HTML report (board render + violation overlays).
"""

from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

_REPO = Path(__file__).resolve().parent.parent
GERBER = _REPO / "testdata" / "mini_board.zip"

pytestmark = pytest.mark.skipif(not GERBER.exists(), reason="gerber fixture missing")


def _no_external_assets(html: str):
    # The SVG xmlns URL is fine; there must be no fetched external assets.
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "@import" not in html
    assert "url(http" not in html


def _result_and_geometry():
    from pcb_dfm.engine.run import build_geometry_for, run_dfm_on_gerber_zip
    result = run_dfm_on_gerber_zip(GERBER, ruleset_id="default", design_id="mini")
    geometry = build_geometry_for(GERBER)
    return result, geometry


def test_html_report_is_self_contained_with_board_and_markers():
    from pcb_dfm.report import generate_html_report
    result, geometry = _result_and_geometry()

    html = generate_html_report(result, geometry)

    assert html.startswith("<!doctype html>")
    assert "<svg" in html                     # board drawn
    assert "<polygon" in html                 # geometry rendered
    assert 'class="marker"' in html           # at least one violation marker
    assert result.summary.status.upper() in html
    assert "<script" in html                  # interactivity present
    _no_external_assets(html)


def test_located_violations_get_markers_and_pins():
    from pcb_dfm.report import generate_html_report
    result, geometry = _result_and_geometry()
    html = generate_html_report(result, geometry)

    n_located = sum(
        1
        for cat in result.categories
        for chk in cat.checks
        for v in chk.violations
        if v.location is not None and v.location.x_mm is not None and v.location.y_mm is not None
    )
    if n_located == 0:
        pytest.skip("fixture produced no located violations")

    assert html.count('class="marker"') == n_located
    # every marker id is referenced by a highlight pin button
    for i in range(n_located):
        assert f'id="m{i}"' in html
        assert f"hl('m{i}')" in html


def test_html_report_without_geometry_still_valid():
    from pcb_dfm.report import generate_html_report
    result, _ = _result_and_geometry()

    html = generate_html_report(result, None)
    assert html.startswith("<!doctype html>")
    assert "No drawable board geometry" in html
    assert "<svg" not in html
    _no_external_assets(html)
