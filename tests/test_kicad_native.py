"""Native .kicad_pcb rendering, with no KiCad installed (#13 Tier 3).

Tier 2 shells out to kicad-cli. This path drives gerbonara's KiCad model, which
already maintains the .kicad_pcb schema, and renders into a gerbonara LayerStack
-- the same primitives our Gerber backend consumes -- then writes it out as a
normal Gerber + Excellon package so the whole ruleset runs.

Two things get real scrutiny here, because both are silent when wrong:

  * the zone-fill guard, which is the reason this tier was deferred
  * coordinate handedness, where gerbonara 1.5 mirrors y for tracks, vias and
    graphics but NOT for footprints -- left alone that puts pads on the wrong
    side of the board with no error raised
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.ingest.kicad_native import (  # noqa: E402
    GEOMETRY_SOURCE_KICAD_NATIVE,
    render_to_gerber_zip,
    zone_fill_state,
)

# A 30 x 20 board. Tracks at y=5 and y=10, a via at (10,15), and a two-pad
# footprint at (15,15) whose pad 3 sits 3 mm further down the board.
_BOARD = """(kicad_pcb (version 20221018) (generator pcbnew)
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal) (31 "B.Cu" signal)
    (36 "B.SilkS" user) (37 "F.SilkS" user)
    (38 "B.Mask" user) (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 30 20) (end 0 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.05))
  (segment (start 5 5) (end 25 5) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 5 10) (end 25 10) (width 0.25) (layer "F.Cu") (net 1))
  (via (at 10 15) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))
  (footprint "R_0805" (layer "F.Cu") (at 15 15)
    (pad "1" smd rect (at -1 0) (size 1.2 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd rect (at 1 0) (size 1.2 1.4) (layers "F.Cu" "F.Paste" "F.Mask"))
    (pad "3" smd rect (at 0 3) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
  )
%ZONE%)
"""

_ZONE_HEAD = """  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.5))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 2 2) (xy 28 2) (xy 28 18) (xy 2 18)))
"""
_ZONE_UNFILLED = _ZONE_HEAD + "  )\n"
_ZONE_FILLED = _ZONE_HEAD + (
    '    (filled_polygon (layer "F.Cu") '
    "(pts (xy 2 2) (xy 28 2) (xy 28 18) (xy 2 18)))\n  )\n"
)


def _write(tmp_path: Path, zone: str = "", name: str = "b.kicad_pcb") -> Path:
    p = tmp_path / name
    p.write_text(_BOARD.replace("%ZONE%", zone))
    return p


def _copper_polys(zip_path):
    from pcb_dfm.engine.run import build_geometry_for

    geom = build_geometry_for(zip_path)
    return [
        p for lyr in geom.get_layers_by_type("copper")
        if lyr.logical_layer == "TopCopper" for p in lyr.polygons
    ]


def _poly_area(p) -> float:
    v = [(q.x, q.y) for q in p.vertices]
    s = 0.0
    for i in range(len(v)):
        x1, y1 = v[i]
        x2, y2 = v[(i + 1) % len(v)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


# --------------------------------------------------------------------------
# The zone-fill guard -- the reason this tier was deferred
# --------------------------------------------------------------------------
def test_unfilled_zones_are_detected(tmp_path):
    """Poured copper lives in the file only as filled_polygon records. Save
    without refilling and the outline remains while the copper does not."""
    state = zone_fill_state(_write(tmp_path, _ZONE_UNFILLED))
    assert state.total == 1
    assert state.filled == 0
    assert not state.ok
    assert "refill zones" in state.describe().lower()


def test_filled_zones_are_recognised(tmp_path):
    state = zone_fill_state(_write(tmp_path, _ZONE_FILLED))
    assert state.total == 1 and state.filled == 1 and state.ok


def test_rendering_refuses_a_board_with_unfilled_zones(tmp_path):
    """Measuring geometry that is missing its pours would be wrong, not merely
    incomplete, so this declines loudly rather than quietly under-reporting."""
    with pytest.raises(RuntimeError, match="poured copper"):
        render_to_gerber_zip(_write(tmp_path, _ZONE_UNFILLED))


def test_the_refusal_can_be_overridden_explicitly(tmp_path):
    """An escape hatch exists, but the caller has to ask for it by name."""
    z = render_to_gerber_zip(_write(tmp_path, _ZONE_UNFILLED), allow_unfilled_zones=True)
    assert z.exists()


def test_a_board_with_no_zones_renders_freely(tmp_path):
    assert zone_fill_state(_write(tmp_path)).ok
    assert render_to_gerber_zip(_write(tmp_path)).exists()


# --------------------------------------------------------------------------
# Coordinate handedness -- silent when wrong
# --------------------------------------------------------------------------
def test_footprint_pads_land_on_the_board_not_mirrored_off_it(tmp_path):
    """Regression for a 30 mm placement error.

    KiCad's y grows downward and Gerber's upward, so rendering must mirror y.
    gerbonara 1.5 does that for tracks, vias and graphics but NOT for
    footprints, which put a footprint at y=15 on the board at +15 while
    everything else sat at -15 -- pads clean off the board, no error raised.
    """
    polys = _copper_polys(render_to_gerber_zip(_write(tmp_path)))
    assert polys, "expected copper"

    ys = [p.bounds().min_y for p in polys] + [p.bounds().max_y for p in polys]
    assert max(ys) <= 0.01, "all copper must be on the mirrored (negative-y) board"

    # Pads: footprint at (15,15) with local x offsets -1/+1 and pad 3 at local
    # (0,+3) -> KiCad (15,18) -> rendered y = -18.
    centres = {
        (round(0.5 * (p.bounds().min_x + p.bounds().max_x), 2),
         round(0.5 * (p.bounds().min_y + p.bounds().max_y), 2))
        for p in polys
    }
    assert (14.0, -15.0) in centres
    assert (16.0, -15.0) in centres
    assert (15.0, -18.0) in centres, "a pad's local y offset must mirror too"


def test_tracks_and_vias_keep_their_positions(tmp_path):
    polys = _copper_polys(render_to_gerber_zip(_write(tmp_path)))
    ys = {round(0.5 * (p.bounds().min_y + p.bounds().max_y), 2) for p in polys}
    assert -5.0 in ys and -10.0 in ys, "tracks at KiCad y=5/10 render at -5/-10"
    assert -15.0 in ys, "via at KiCad y=15 renders at -15"


def test_zone_fill_is_rendered_at_its_true_area(tmp_path):
    """The poured copper is emitted, and the zone OUTLINE never stands in for it."""
    polys = _copper_polys(render_to_gerber_zip(_write(tmp_path, _ZONE_FILLED)))
    # Zone spans 2..28 by 2..18 = 26 x 16 = 416 mm^2.
    assert any(abs(_poly_area(p) - 416.0) < 1.0 for p in polys)


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------
def test_running_straight_from_a_board_file_records_its_provenance(tmp_path):
    """`pcb-dfm run board.kicad_pcb` with nothing installed."""
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    res = run_dfm_on_gerber_zip(_write(tmp_path, _ZONE_FILLED), ruleset_id="default")
    assert res.summary.geometry_source == GEOMETRY_SOURCE_KICAD_NATIVE
    assert any("export-time" in w for w in res.warnings), (
        "a design-file run must not imply it audited the user's fab package"
    )

    ran = [c for cat in res.categories for c in cat.checks
           if c.status in ("pass", "warning", "fail")]
    assert len(ran) >= 20, f"only {len(ran)} checks produced a verdict"


def test_the_layers_a_board_needs_all_come_through(tmp_path):
    from pcb_dfm.ingest import ingest_gerber_zip

    files = ingest_gerber_zip(render_to_gerber_zip(_write(tmp_path))).files
    kinds = {f.layer_type for f in files}
    for needed in ("copper", "mask", "silkscreen", "drill", "outline"):
        assert needed in kinds, f"{needed} missing from the rendered package"
