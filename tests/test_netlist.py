"""IPC-D-356 netlist ingest, and the capability it buys back.

Several checks were made advisory because, without a netlist, they cannot tell
copper a via CONNECTS to from a foreign net it must clear. This pins the way
back: parse a netlist, register it to the board, label copper by net, and let
genuine different-net violations fail again.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import boards  # tests/boards.py
import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.ingest.adapters.ipc356 import (  # noqa: E402
    from_ipc356,
    looks_like_ipc356,
    register_to_board,
)
from pcb_dfm.ingest.design_model import DesignData, Net, NetPoint  # noqa: E402

# A minimal IPC-D-356 file. "UNITS CUST 0" = 0.0001 inch per count, so
# X 10000 = 1.0000 in = 25.4 mm.
_NETLIST = """C  IPC-D-356 test netlist
P  UNITS CUST 0
317GND              VIA         D  24PA00X  10000Y   5000X 396Y 396
317VCC              VIA         D  24PA00X  20000Y   5000X 396Y 396
317GND              VIA         D  24PA00X  40000Y   5000X 396Y 396
327SIG             R1    -1     D   0PA00X  30000Y  10000X 600Y 600
999
"""


def _write(tmp_path: Path, text: str = _NETLIST) -> Path:
    p = tmp_path / "board.ipc"
    p.write_text(text)
    return p


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def test_detects_ipc356_by_content_not_extension(tmp_path):
    """The .ipc extension is shared with IPC-2581, so detection must read the
    file; otherwise one format silently parses as the other."""
    p = _write(tmp_path)
    assert looks_like_ipc356(p)

    ipc2581 = tmp_path / "other.ipc"
    ipc2581.write_text('<?xml version="1.0"?><IPC-2581 revision="B"></IPC-2581>')
    assert not looks_like_ipc356(ipc2581)


def test_parses_nets_points_and_units(tmp_path):
    d = from_ipc356(_write(tmp_path))
    assert set(d.nets) == {"GND", "VCC", "SIG"}

    gnd = d.nets["GND"].points[0]
    assert gnd.x_mm == pytest.approx(25.4)      # 10000 * 0.0001 in
    assert gnd.y_mm == pytest.approx(12.7)
    assert gnd.kind == "through"                # 317 record

    sig = d.nets["SIG"].points[0]
    assert sig.kind == "smd"                    # 327 record
    assert sig.x_mm == pytest.approx(76.2)


def test_metric_units_are_honoured(tmp_path):
    p = _write(tmp_path, _NETLIST.replace("UNITS CUST 0", "UNITS METRIC 2"))
    d = from_ipc356(p)
    # METRIC -> 0.001 mm per count, so 10000 counts = 10 mm.
    assert d.nets["GND"].points[0].x_mm == pytest.approx(10.0)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
def test_registration_finds_and_applies_the_board_offset(tmp_path):
    """Netlist coordinates are commonly relative to the board's corner, not the
    Gerber origin. The offset must be derived from the drill hits, not assumed.
    """
    d = from_ipc356(_write(tmp_path))
    # Pretend the board's drills sit 5 mm right and 3 mm up from the netlist frame.
    drills = [
        (25.4 + 5.0, 12.7 + 3.0),
        (50.8 + 5.0, 12.7 + 3.0),
        (101.6 + 5.0, 12.7 + 3.0),
    ]
    off = register_to_board(d, drills)
    assert off is not None
    dx, dy = off
    assert dx == pytest.approx(5.0, abs=1e-6)
    assert dy == pytest.approx(3.0, abs=1e-6)
    assert d.nets["GND"].points[0].x_mm == pytest.approx(30.4)


def test_registration_refuses_a_netlist_for_a_different_board(tmp_path):
    """A partly-wrong origin is worse than no netlist, so registration must fail
    closed rather than align a handful of coincidental pairs."""
    d = from_ipc356(_write(tmp_path))
    before = d.nets["GND"].points[0].x_mm
    # Drills that match nothing in the netlist.
    assert register_to_board(d, [(3.0, 3.0), (7.0, 9.0), (11.0, 13.0)]) is None
    assert d.nets["GND"].points[0].x_mm == before, "coordinates must be untouched"


# --------------------------------------------------------------------------
# The capability this buys back
# --------------------------------------------------------------------------
def _run(board, design_data):
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    with tempfile.TemporaryDirectory() as td:
        z = boards.emit_zip(board, Path(td), name="b.zip")
        res = run_dfm_on_gerber_zip(z, ruleset_id="default", design_data=design_data)
    return {c.check_id: c for cat in res.categories for c in cat.checks}


def _two_pad_board():
    """A via in the left pad, sitting 0.08 mm from the right pad's copper.

    Via centre x=9.0, barrel r=0.15 -> barrel edge at 9.15. The right pad's near
    edge is at 9.23, so the barrel-to-copper clearance is 0.08 mm, below the
    0.10 mm absolute minimum. The two pads themselves stay 0.03 mm apart, so the
    board is tight but not shorted.
    """
    return boards.Board(
        outline=[(0, 0), (20, 0), (20, 14), (0, 14)],
        pads=[boards.Pad(9.0, 7.0, 0.4, 0.4), boards.Pad(9.43, 7.0, 0.4, 0.4)],
        holes=[boards.Hole(9.0, 7.0, 0.3)],
    )


def test_foreign_net_violation_fails_once_a_netlist_is_supplied():
    """With net data, a via too close to a DIFFERENT net is a real defect again.

    This is the capability the advisory downgrade was holding in trust: without
    a netlist the same geometry is indistinguishable from a via sitting next to
    copper it connects to, so it could only warn.
    """
    board = _two_pad_board()
    dd = DesignData(nets={
        "NET_A": Net(name="NET_A", points=[NetPoint(x_mm=9.0, y_mm=7.0)]),
        "NET_B": Net(name="NET_B", points=[NetPoint(x_mm=9.43, y_mm=7.0)]),
    })
    c = _run(board, dd)["via_to_copper_clearance"]
    assert c.status == "fail", "a known different-net violation must fail"


def test_same_geometry_only_warns_without_a_netlist():
    """The control: identical board, no net data -> advisory, exactly as before."""
    c = _run(_two_pad_board(), None)["via_to_copper_clearance"]
    assert c.status != "fail"


def test_same_net_copper_is_not_a_violation():
    """Two pads on the SAME net are copper the via connects to, not a clearance
    defect -- the dominant false positive a netlist removes."""
    board = _two_pad_board()
    dd = DesignData(nets={
        "NET_A": Net(name="NET_A", points=[
            NetPoint(x_mm=9.0, y_mm=7.0),
            NetPoint(x_mm=9.43, y_mm=7.0),
        ]),
    })
    c = _run(board, dd)["via_to_copper_clearance"]
    assert c.status != "fail"


# --------------------------------------------------------------------------
# Real artwork: the pcb-tools reference board and its own netlist
# --------------------------------------------------------------------------
_BOARD = Path(__file__).resolve().parent.parent / "testdata" / "pcbtools_full.zip"
_IPC = Path(__file__).resolve().parent.parent / "testdata" / "pcbtools_full.ipc"

_real = pytest.mark.skipif(
    not (_BOARD.exists() and _IPC.exists()), reason="corpus board/netlist missing"
)


@_real
def test_real_netlist_registers_and_labels_copper_by_net():
    """End to end on real artwork: parse, auto-register, label copper.

    The netlist states coordinates relative to the board's lower-left corner, so
    this also proves the offset is recovered rather than assumed -- every one of
    its through-hole records has to land on a real drill hit for the labelling
    to mean anything.
    """
    from pcb_dfm.engine.run import build_geometry_for
    from pcb_dfm.geometry.gerber_backend import excellon_hits_mm
    from pcb_dfm.geometry.net_map import build_net_map
    from pcb_dfm.ingest import ingest_gerber_zip
    from pcb_dfm.ingest.design_data import load_design_data

    dd = load_design_data(str(_IPC))
    assert len(dd.nets) > 5

    drills = [
        (h.x_mm, h.y_mm)
        for f in ingest_gerber_zip(_BOARD).files if f.layer_type == "drill"
        for h in excellon_hits_mm(f.path)
    ]
    through = [p for n in dd.nets.values() for p in n.points if p.kind == "through"]
    off = register_to_board(dd, drills)
    assert off is not None, "the netlist must register onto this board"

    landed = sum(
        1 for p in through
        if any(abs(p.x_mm - bx) < 0.06 and abs(p.y_mm - by) < 0.06 for (bx, by) in drills)
    )
    assert landed == len(through), f"only {landed}/{len(through)} records landed on a drill"

    net_map = build_net_map(build_geometry_for(_BOARD), dd)
    assert net_map is not None
    assert net_map.tagged_polygon_count() > 300, "copper should be labelled by net"


@_real
def test_real_board_stays_advisory_without_a_known_foreign_violation():
    """Supplying a netlist must not manufacture failures.

    On this board the tightest clearances are to copper the netlist does not
    label (traces carry no access point), and unknown is not foreign -- so the
    check reports, but does not fail.
    """
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    res = run_dfm_on_gerber_zip(_BOARD, ruleset_id="default", design_data=str(_IPC))
    c = {c.check_id: c for cat in res.categories for c in cat.checks}["via_to_copper_clearance"]
    assert c.status != "fail"
