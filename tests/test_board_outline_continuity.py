"""Correctness tests for the board_outline_continuity check.

An open board outline (Edge.Cuts that does not close) is a hard fab reject: the
fabricator cannot determine the board boundary. This check must:

  * PASS when the outline segments chain into at least one closed loop, even if
    stray dangling ends (dimension lines, plot marks) are also present -- the
    #18 lesson: a closed boundary plus a stray line is still routable.
  * FAIL when outline geometry exists but nothing closes and the gap is beyond
    the fab's join tolerance.
  * WARN when the only gap is within join tolerance (auto-closeable).
  * be not_applicable when no outline layer is present at all.

Artwork is synthesized at runtime (see test_check_correctness.py for format
notes: %FSLAX46Y46*% + %MOMM*%, integer tokens are mm * 1e6).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check


def _make_zip(tmp_path: Path, files: dict[str, str]) -> Path:
    zip_path = tmp_path / "board.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    return zip_path


def _copper_trace(width_mm: float = 0.25) -> str:
    return (
        "%FSLAX46Y46*%\n%MOMM*%\n"
        f"%ADD10C,{width_mm:.6f}*%\nD10*\n"
        "X1000000Y1000000D02*\nX9000000Y1000000D01*\nM02*\n"
    )


def _seg(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f"X{int(round(x1 * 1e6))}Y{int(round(y1 * 1e6))}D02*\n"
        f"X{int(round(x2 * 1e6))}Y{int(round(y2 * 1e6))}D01*\n"
    )


def _outline(*segments: str) -> str:
    body = "".join(segments)
    return "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.010000*%\nD10*\n" + body + "M02*\n"


# A closed 10x10 rectangle.
_CLOSED = _outline(
    _seg(0, 0, 10, 0), _seg(10, 0, 10, 10), _seg(10, 10, 0, 10), _seg(0, 10, 0, 0)
)
# Same rectangle missing its left edge -> a 10 mm gap, nothing closes.
_OPEN = _outline(
    _seg(0, 0, 10, 0), _seg(10, 0, 10, 10), _seg(10, 10, 0, 10)
)
# Left edge stops 0.03 mm short of closing -> within the 0.05 mm join tolerance.
_NEARLY = _outline(
    _seg(0, 0, 10, 0), _seg(10, 0, 10, 10), _seg(10, 10, 0, 10), _seg(0, 10, 0, 0.03)
)
# A closed rectangle PLUS a detached dimension line off to the side.
_CLOSED_PLUS_STRAY = _outline(
    _seg(0, 0, 10, 0), _seg(10, 0, 10, 10), _seg(10, 10, 0, 10), _seg(0, 10, 0, 0),
    _seg(20, 20, 25, 20),
)


def _run(tmp_path: Path, outline: str | None):
    files = {"board.gtl": _copper_trace()}
    if outline is not None:
        files["board.gko"] = outline
    z = _make_zip(tmp_path, files)
    return run_single_check(z, load_check_definition("board_outline_continuity"))


def test_closed_outline_passes(tmp_path):
    r = _run(tmp_path, _CLOSED)
    assert r.status == "pass"
    assert r.metric.measured_value == pytest.approx(0.0)


def test_open_outline_fails_with_measured_gap(tmp_path):
    r = _run(tmp_path, _OPEN)
    assert r.status == "fail"
    # The gap to close is the missing left edge: 10 mm.
    assert r.metric.measured_value == pytest.approx(10.0, abs=0.05)


def test_nearly_closed_outline_warns(tmp_path):
    r = _run(tmp_path, _NEARLY)
    assert r.status == "warning"
    assert r.metric.measured_value == pytest.approx(0.03, abs=0.01)


def test_closed_plus_stray_line_still_passes(tmp_path):
    """The #18 lesson: a closed boundary is routable even with stray marks."""
    r = _run(tmp_path, _CLOSED_PLUS_STRAY)
    assert r.status == "pass"
    assert r.metric.measured_value == pytest.approx(0.0)


def test_no_outline_layer_is_not_applicable(tmp_path):
    r = _run(tmp_path, None)
    assert r.status == "not_applicable"


# A genuinely open outline (missing left edge -> 10 mm gap) alongside a stray
# dimension line whose own two ends sit 0.02 mm apart. The smallest gap over ALL
# dangling ends is the 0.02 mm stray pair, which must NOT downgrade the hard
# reject to a warning: with more than one clean break (4 dangling ends) the
# result is a fail regardless of the coincidental stray proximity.
_OPEN_PLUS_CLOSE_STRAY = _outline(
    _seg(0, 0, 10, 0), _seg(10, 0, 10, 10), _seg(10, 10, 0, 10),
    _seg(20, 20, 25, 20), _seg(25, 20.02, 30, 20.02),
)


def test_open_outline_not_masked_by_close_stray_endpoints(tmp_path):
    r = _run(tmp_path, _OPEN_PLUS_CLOSE_STRAY)
    assert r.status == "fail"
