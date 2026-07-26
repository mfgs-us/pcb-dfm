"""Correctness tests for the first two Tier-2 (design-data-gated) checks:
test_point_coverage (#36) and antenna_keepout (#39).

Both are advisory and gated: not_applicable without the design data they need,
warning when they find something, pass when clean.
"""

from __future__ import annotations

import zipfile

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check
from pcb_dfm.ingest.design_model import DesignData, KeepoutRegion, Net, NetPoint

_HDR = "%FSLAX46Y46*%\n%MOMM*%\n"


def _zip(tmp_path, files):
    p = tmp_path / "b.zip"
    with zipfile.ZipFile(p, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    return p


def _flash(cx, cy, w, h, ap="11"):
    return (_HDR + f"%ADD{ap}R,{w:.6f}X{h:.6f}*%\nD{ap}*\n"
            f"X{int(round(cx * 1e6))}Y{int(round(cy * 1e6))}D03*\nM02*\n")


def _outline(w, h):
    return (_HDR + "%ADD10C,0.010000*%\nD10*\n"
            f"X0Y0D02*\nX{int(w * 1e6)}Y0D01*\nX{int(w * 1e6)}Y{int(h * 1e6)}D01*\n"
            f"X0Y{int(h * 1e6)}D01*\nX0Y0D01*\nM02*\n")


def _run(z, cid, dd=None):
    return run_single_check(z, load_check_definition(cid), design_data=dd)


# ---- test_point_coverage --------------------------------------------------
def test_test_point_not_applicable_without_netlist(tmp_path):
    z = _zip(tmp_path, {"board.gtl": _flash(5, 5, 1, 1), "board.gts": _flash(5, 5, 1.2, 1.2)})
    assert _run(z, "test_point_coverage").status == "not_applicable"


def test_test_point_exposed_net_passes(tmp_path):
    # Copper pad + a mask opening at (5,5); a net with its access point there.
    z = _zip(tmp_path, {"board.gtl": _flash(5, 5, 1, 1), "board.gts": _flash(5, 5, 1.2, 1.2)})
    dd = DesignData()
    dd.nets["A"] = Net(name="A", points=[NetPoint(x_mm=5.0, y_mm=5.0)])
    assert _run(z, "test_point_coverage", dd).status == "pass"


def test_test_point_covered_net_warns(tmp_path):
    # Copper at BOTH (5,5) and (9,9) so both access points are registered; the
    # mask opening is only at (5,5), so net B's pad at (9,9) is tented (on copper,
    # not exposed) -> untestable. (B must be on copper, else it reads as a
    # mis-registered netlist and is skipped.)
    copper = (_HDR + "%ADD11R,1.000000X1.000000*%\nD11*\n"
              "X5000000Y5000000D03*\nX9000000Y9000000D03*\nM02*\n")
    z = _zip(tmp_path, {"board.gtl": copper, "board.gts": _flash(5, 5, 1.2, 1.2)})
    dd = DesignData()
    dd.nets["A"] = Net(name="A", points=[NetPoint(x_mm=5.0, y_mm=5.0)])
    dd.nets["B"] = Net(name="B", points=[NetPoint(x_mm=9.0, y_mm=9.0)])
    r = _run(z, "test_point_coverage", dd)
    assert r.status == "warning"
    assert r.metric.measured_value == 1


# ---- antenna_keepout ------------------------------------------------------
def test_antenna_keepout_not_applicable_without_region(tmp_path):
    z = _zip(tmp_path, {"board.gtl": _flash(5, 5, 2, 2), "board.gko": _outline(20, 20)})
    assert _run(z, "antenna_keepout").status == "not_applicable"


def test_antenna_keepout_copper_in_region_warns(tmp_path):
    # A copper flash at (5,5); an antenna keep-out covering it.
    z = _zip(tmp_path, {"board.gtl": _flash(5, 5, 2, 2), "board.gko": _outline(20, 20)})
    dd = DesignData(keepouts=[KeepoutRegion(
        kind="antenna", name="BLE", polygon=[(3, 3), (8, 3), (8, 8), (3, 8)])])
    r = _run(z, "antenna_keepout", dd)
    assert r.status == "warning"
    assert r.metric.measured_value >= 1


def test_antenna_keepout_clear_region_passes(tmp_path):
    # Copper at (5,5); keep-out region far away in an empty corner.
    z = _zip(tmp_path, {"board.gtl": _flash(5, 5, 2, 2), "board.gko": _outline(20, 20)})
    dd = DesignData(keepouts=[KeepoutRegion(
        kind="antenna", name="BLE", polygon=[(15, 15), (19, 15), (19, 19), (15, 19)])])
    assert _run(z, "antenna_keepout", dd).status == "pass"
