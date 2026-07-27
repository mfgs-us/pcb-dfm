"""Net-label coverage regression guard.

Net-aware checks are only as good as the fraction of copper the net map labels.
This pins a floor so a regression in seeding / conductor merge / registration is
caught -- and it measures coverage *the way the engine does*, i.e. with the
netlist registered to the board first. (Skipping registration collapses coverage
to a few percent, which once sent an entire investigation chasing a residual that
did not exist -- see docs/connectivity_model.md.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.geometry import build_board_geometry
from pcb_dfm.geometry.gerber_backend import excellon_hits_mm
from pcb_dfm.geometry.net_map import build_net_map
from pcb_dfm.ingest import ingest_gerber_zip
from pcb_dfm.ingest.adapters.ipc356 import register_to_board
from pcb_dfm.ingest.design_data import load_design_data

_TESTDATA = Path(__file__).resolve().parent.parent / "testdata"


def _drills(zip_path: Path):
    import os
    import tempfile
    import zipfile
    d = tempfile.mkdtemp()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(d)
    hits = []
    for f in os.listdir(d):
        if f.lower().endswith((".drl", ".txt", ".xln")) or "drill" in f.lower():
            try:
                hits += [(h.x_mm, h.y_mm) for h in excellon_hits_mm(Path(os.path.join(d, f)))]
            except Exception:
                pass
    return hits


def test_pcbtools_net_coverage_high_after_registration():
    gerbers = _TESTDATA / "pcbtools_full.zip"
    netlist = _TESTDATA / "pcbtools_full.ipc"
    geom = build_board_geometry(ingest_gerber_zip(gerbers))
    dd = load_design_data(netlist)

    # Un-registered, the IPC-D-356 points sit ~7.8 mm off the copper -> near-zero.
    assert build_net_map(geom, dd).coverage() < 0.10

    # Registered (as the engine's _auto_register_netlist does) -> the netlist
    # lands on the copper and the vast majority of it is net-labelled.
    register_to_board(dd, _drills(gerbers))
    nm = build_net_map(geom, dd)
    assert nm.coverage() > 0.90, f"net coverage regressed to {nm.coverage():.0%}"
