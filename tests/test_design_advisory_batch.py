"""Correctness tests for the batch of design-advisory checks:
mounting_hole_keepout, fine_pitch_fiducials, power_ground_trace_width,
courtyard_overlap.

All are advisory and gated: not_applicable without the data they need, warning
when they find something, pass when clean. The suite pins the false-positive
guards discovered on real boards (flat fiducials / mounting holes are not
collision bodies; a single pad-entry neck must not trip the width check).
"""

from __future__ import annotations

import zipfile

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check
from pcb_dfm.ingest.design_model import (
    Component,
    DesignData,
    Net,
    NetFeature,
    Pad,
)

_HDR = "%FSLAX46Y46*%\n%MOMM*%\n"


def _flash(cx, cy, w, h):
    return (_HDR + f"%ADD11R,{w:.6f}X{h:.6f}*%\nD11*\n"
            f"X{int(round(cx * 1e6))}Y{int(round(cy * 1e6))}D03*\nM02*\n")


def _npth(cx, cy, dia_mm):
    return f"M48\nMETRIC,TZ\nT1C{dia_mm:.3f}\n%\nT1\nX{cx:.3f}Y{cy:.3f}\nT0\nM30\n"


def _zip(tmp_path, extra=None):
    p = tmp_path / "b.zip"
    files = {"board.gtl": _flash(5, 5, 1, 1), "board.gts": _flash(5, 5, 1.2, 1.2)}
    files.update(extra or {})
    with zipfile.ZipFile(p, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    return p


def _run(z, cid, dd):
    return run_single_check(z, load_check_definition(cid), design_data=dd)


def _comp(ref, pads, courtyard=None, side=None, footprint=None):
    return Component(ref=ref, footprint=footprint, side=side, courtyard=courtyard,
                     pads=[Pad(name=n, x_mm=x, y_mm=y) for (n, x, y) in pads])


# ---- mounting_hole_keepout -------------------------------------------------
def test_mounting_na_without_npth(tmp_path):
    dd = DesignData(components=[_comp("R1", [("1", 12, 10)])])
    assert _run(_zip(tmp_path), "mounting_hole_keepout", dd).status == "not_applicable"


def test_mounting_intruding_component_warns(tmp_path):
    # 3 mm NPTH at (10,10) -> keep-out radius 1.5 + 2.5 = 4 mm. R1 at (13,10) is
    # inside the keep-out but outside the owner tolerance (so not the hole's own part).
    z = _zip(tmp_path, {"board-NPTH.drl": _npth(10, 10, 3.0)})
    dd = DesignData(components=[_comp("R1", [("1", 13, 10)])])
    r = _run(z, "mounting_hole_keepout", dd)
    assert r.status == "warning" and r.metric.measured_value == 1


def test_mounting_clear_component_passes(tmp_path):
    z = _zip(tmp_path, {"board-NPTH.drl": _npth(10, 10, 3.0)})
    dd = DesignData(components=[_comp("R1", [("1", 10, 20)])])  # 10 mm away
    assert _run(z, "mounting_hole_keepout", dd).status == "pass"


def test_mounting_small_npth_ignored(tmp_path):
    # A 1 mm NPTH (connector peg) is not a mounting hole -> not_applicable.
    z = _zip(tmp_path, {"board-NPTH.drl": _npth(10, 10, 1.0)})
    dd = DesignData(components=[_comp("R1", [("1", 10.3, 10)])])
    assert _run(z, "mounting_hole_keepout", dd).status == "not_applicable"


def test_mounting_flat_fiducial_not_flagged(tmp_path):
    # A fiducial next to a mounting hole is flat -> no collision.
    z = _zip(tmp_path, {"board-NPTH.drl": _npth(10, 10, 3.0)})
    dd = DesignData(components=[_comp("FID1", [("1", 11, 10)])])
    assert _run(z, "mounting_hole_keepout", dd).status == "not_applicable"


# ---- fine_pitch_fiducials --------------------------------------------------
def _fine_ic():
    return _comp("U1", [(str(i), 0.5 * i, 0) for i in range(5)])  # 0.5 mm pitch, 5 pads


def test_fine_pitch_na_without_fiducials(tmp_path):
    dd = DesignData(components=[_fine_ic()])
    assert _run(_zip(tmp_path), "fine_pitch_fiducials", dd).status == "not_applicable"


def test_fine_pitch_local_pair_passes(tmp_path):
    dd = DesignData(components=[_fine_ic(),
                               _comp("FID1", [("1", 5, 0)]),
                               _comp("FID2", [("1", 6, 0)])])
    assert _run(_zip(tmp_path), "fine_pitch_fiducials", dd).status == "pass"


def test_fine_pitch_distant_fiducials_warn(tmp_path):
    # Only one fiducial, and it is 100 mm away -> no local pair.
    dd = DesignData(components=[_fine_ic(), _comp("FID1", [("1", 100, 100)])])
    r = _run(_zip(tmp_path), "fine_pitch_fiducials", dd)
    assert r.status == "warning" and r.metric.measured_value == 1


def test_fine_pitch_coarse_part_not_evaluated(tmp_path):
    # A 1.27 mm-pitch part is not fine-pitch -> not_applicable (nothing to judge).
    coarse = _comp("U2", [(str(i), 1.27 * i, 0) for i in range(5)])
    dd = DesignData(components=[coarse, _comp("FID1", [("1", 3, 0)])])
    assert _run(_zip(tmp_path), "fine_pitch_fiducials", dd).status == "not_applicable"


# ---- power_ground_trace_width ----------------------------------------------
def _seg_net(name, width, net_class=None, n=1):
    feats = [NetFeature(layer="F.Cu", length_mm=5.0, width_mm=width,
                        segments=[((0, 0), (5, 0))]) for _ in range(n)]
    return Net(name=name, net_class=net_class, features=feats)


def _baseline_signals():
    return {f"SIG{i}": _seg_net(f"SIG{i}", 0.25) for i in range(6)}


def test_power_width_na_without_baseline(tmp_path):
    dd = DesignData()
    dd.nets["VCC"] = _seg_net("VCC", 0.15)  # no signal baseline
    assert _run(_zip(tmp_path), "power_ground_trace_width", dd).status == "not_applicable"


def test_power_width_thin_rail_warns(tmp_path):
    dd = DesignData()
    dd.nets.update(_baseline_signals())            # median signal 0.25 mm
    dd.nets["VCC"] = _seg_net("VCC", 0.15)          # thinner -> bottleneck
    r = _run(_zip(tmp_path), "power_ground_trace_width", dd)
    assert r.status == "warning" and r.metric.measured_value == 1


def test_power_width_adequate_rail_passes(tmp_path):
    dd = DesignData()
    dd.nets.update(_baseline_signals())
    dd.nets["GND"] = _seg_net("GND", 0.5)           # wider than signal -> fine
    assert _run(_zip(tmp_path), "power_ground_trace_width", dd).status == "pass"


# ---- courtyard_overlap -----------------------------------------------------
def test_courtyard_na_without_geometry(tmp_path):
    dd = DesignData(components=[_comp("R1", [("1", 0, 0)])])  # no courtyard
    assert _run(_zip(tmp_path), "courtyard_overlap", dd).status == "not_applicable"


def test_courtyard_overlap_warns(tmp_path):
    dd = DesignData(components=[
        _comp("U1", [("1", 0, 0)], courtyard=(0, 0, 4, 4), side="top"),
        _comp("U2", [("1", 3, 3)], courtyard=(2, 2, 6, 6), side="top"),
    ])
    r = _run(_zip(tmp_path), "courtyard_overlap", dd)
    assert r.status == "warning" and r.metric.measured_value == 1


def test_courtyard_clear_passes(tmp_path):
    dd = DesignData(components=[
        _comp("U1", [("1", 0, 0)], courtyard=(0, 0, 4, 4), side="top"),
        _comp("U2", [("1", 10, 10)], courtyard=(8, 8, 12, 12), side="top"),
    ])
    assert _run(_zip(tmp_path), "courtyard_overlap", dd).status == "pass"


def test_courtyard_opposite_sides_not_compared(tmp_path):
    dd = DesignData(components=[
        _comp("U1", [("1", 0, 0)], courtyard=(0, 0, 4, 4), side="top"),
        _comp("U2", [("1", 3, 3)], courtyard=(2, 2, 6, 6), side="bottom"),
    ])
    assert _run(_zip(tmp_path), "courtyard_overlap", dd).status == "pass"


def test_courtyard_fiducial_excluded(tmp_path):
    # FID1's courtyard overlaps U1, but a flat fiducial is not a placement
    # collision -> excluded. The two real bodies (U1, U2) do not overlap -> pass.
    dd = DesignData(components=[
        _comp("U1", [("1", 0, 0)], courtyard=(0, 0, 4, 4), side="top"),
        _comp("U2", [("1", 10, 10)], courtyard=(8, 8, 12, 12), side="top"),
        _comp("FID1", [("1", 3, 3)], courtyard=(2, 2, 6, 6), side="top"),
    ])
    assert _run(_zip(tmp_path), "courtyard_overlap", dd).status == "pass"
