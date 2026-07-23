"""Net labels cross layers through plated vias (#20).

A net is a three-dimensional object: a bottom-layer trace routed away from a via
belongs to the same net as the top-layer copper on the other side of it.
Propagating labels only within each layer leaves such a trace unlabelled, and an
unlabelled polygon is useless to a net-aware check -- it can be called neither
same-net nor foreign.

The bridge is the plated through-hole, which is one conductor spanning every
layer it passes through. Unplated holes must not bridge: a mounting hole
connects nothing, and joining nets across one would merge unrelated copper.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.geometry.net_map import build_net_map  # noqa: E402
from pcb_dfm.ingest.design_model import DesignData, Net, NetPoint  # noqa: E402

_H = "%FSLAX46Y46*%\n%MOMM*%\n"
# A 1.5 mm pad at (5,5) on top; a 0.4 mm trace running from (5,5) to (12,5) on
# the bottom. They meet only at the via location, on opposite layers.
_TOP = _H + "%ADD10C,1.500000*%\nD10*\nX5000000Y5000000D03*\nM02*\n"
_BOTTOM = (_H + "%ADD11C,0.400000*%\nD11*\n"
           "X5000000Y5000000D02*\nX12000000Y5000000D01*\nM02*\n")
_OUTLINE = (_H + "%ADD12C,0.100000*%\nD12*\nX0Y0D02*\nX16000000Y0D01*\n"
            "X16000000Y10000000D01*\nX0Y10000000D01*\nX0Y0D01*\nM02*\n")
_DRILL = "M48\nMETRIC,TZ\nT1C0.4000\n%\nT1\nX5.0000Y5.0000\nT0\nM30\n"


def _board(drill_name: str = "board.drl") -> Path:
    td = Path(tempfile.mkdtemp())
    z = td / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("board.gtl", _TOP)
        zf.writestr("board.gbl", _BOTTOM)
        zf.writestr("board.gko", _OUTLINE)
        zf.writestr(drill_name, _DRILL)
    return z


def _design(net="NET_A"):
    """One TOP-side access point on the pad. Nothing names the bottom trace, so
    it can only be labelled by crossing the via."""
    return DesignData(nets={net: Net(name=net, points=[
        NetPoint(x_mm=5.0, y_mm=5.0, kind="smd", layer="top"),
    ])})


def _bottom_labelled(geom, net_map) -> int:
    return sum(
        1 for lyr in geom.get_layers_by_type("copper")
        if lyr.logical_layer == "BottomCopper"
        for p in lyr.polygons if net_map and net_map.net_of(p)
    )


def test_bottom_trace_inherits_the_net_through_a_plated_via():
    from pcb_dfm.engine.run import build_geometry_for

    geom = build_geometry_for(_board())

    without = build_net_map(geom, _design(), [])
    assert _bottom_labelled(geom, without) == 0, (
        "without a bridge the bottom trace has nothing naming it"
    )

    with_bridge = build_net_map(geom, _design(), [(5.0, 5.0)])
    assert _bottom_labelled(geom, with_bridge) == 1, (
        "a plated via ties the bottom trace to the top net"
    )


def test_unplated_holes_are_not_offered_as_bridges():
    """An NPTH passes through the board without connecting anything."""
    from pcb_dfm.engine.run import build_geometry_for
    from pcb_dfm.geometry.net_map import _plated_vias_from_ingest
    from pcb_dfm.ingest import ingest_gerber_zip

    class _Ctx:
        pass

    # A drill file the ingest classifies as non-plated must contribute no bridges.
    z = _board(drill_name="board-NPTH.drl")
    ctx = _Ctx()
    ctx.ingest = ingest_gerber_zip(z)
    ctx.geometry = build_geometry_for(z)

    for f in ctx.ingest.files:
        if f.layer_type == "drill" and f.is_plated is False:
            break
    else:
        pytest.skip("ingest did not classify this drill file as non-plated")

    assert _plated_vias_from_ingest(ctx) is None, "an NPTH must not bridge nets"


def test_a_conductor_with_conflicting_labels_stays_unlabelled():
    """Two different nets on one conductor means the netlist and the artwork
    disagree. Guessing a winner would be worse than staying silent."""
    from pcb_dfm.engine.run import build_geometry_for

    geom = build_geometry_for(_board())
    # Name the top pad NET_A and the bottom trace NET_B, then bridge them: the
    # via would merge two differently-named conductors.
    dd = DesignData(nets={
        "NET_A": Net(name="NET_A", points=[
            NetPoint(x_mm=5.0, y_mm=5.0, kind="smd", layer="top")]),
        "NET_B": Net(name="NET_B", points=[
            NetPoint(x_mm=10.0, y_mm=5.0, kind="smd", layer="bottom")]),
    })
    nm = build_net_map(geom, dd, [(5.0, 5.0)])

    # The bottom trace keeps its own directly-stated label; the merge must not
    # relabel it or spread a contested name.
    bottom = [p for lyr in geom.get_layers_by_type("copper")
              if lyr.logical_layer == "BottomCopper" for p in lyr.polygons]
    assert all(nm.net_of(p) in (None, "NET_B") for p in bottom)


def test_bridging_never_reduces_coverage():
    """Bridging may only add labels; it must not disturb existing ones."""
    from pcb_dfm.engine.run import build_geometry_for

    geom = build_geometry_for(_board())
    plain = build_net_map(geom, _design(), [])
    bridged = build_net_map(geom, _design(), [(5.0, 5.0)])
    assert bridged.tagged_polygon_count() >= plain.tagged_polygon_count()
