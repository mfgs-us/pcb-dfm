"""Correctness tests for the cutout-aware component checks.

  * ``component_cutout_present`` -- the artwork is missing a cutout the design
    data declares, i.e. a fab package out of sync with the board (#105)
  * ``pad_over_cutout``          -- a pad sitting over an internal void (#106)
  * the #107 fix: ``tall_part_edge_clearance`` and ``component_edge_clearance``
    treating internal cutouts (and a concave boundary) as real board edges

The KiCad ingest half is exercised against a real board file, because the
requirement is read out of the design file -- a synthetic ``Component`` with the
field pre-filled would not prove the footprint graphics are read, transformed and
Y-flipped correctly.

The last three tests pin the *scope* of #105, which was narrowed after testing it
on a real board: KiCad plots a footprint's Edge.Cuts graphics into the outline
layer itself, so same-source runs pass trivially and a footprint that declares
nothing is invisible to the check by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import Component, DesignData, Pad

# A board with one mid-mount-style footprint that declares a 6 x 4 mm opening at
# its origin, drawn on Edge.Cuts inside the footprint as four fp_lines.
_BOARD = """(kicad_pcb (version 20221018)
  (footprint "Connector:USB_C_Mid_Mount" (layer "F.Cu") (at 20 15 0)
    (property "Reference" "J1")
    (fp_line (start -3 -2) (end 3 -2) (layer "Edge.Cuts"))
    (fp_line (start 3 -2) (end 3 2) (layer "Edge.Cuts"))
    (fp_line (start 3 2) (end -3 2) (layer "Edge.Cuts"))
    (fp_line (start -3 2) (end -3 -2) (layer "Edge.Cuts"))
    (fp_line (start -4 -3) (end 4 -3) (layer "F.CrtYd"))
    (pad "1" smd rect (at -2.5 3) (size 0.6 0.8) (layers "F.Cu"))
  )
)
"""


def _write_board(tmp_path: Path, text: str = _BOARD) -> Path:
    d = tmp_path / "brd"
    d.mkdir(exist_ok=True)
    p = d / "brd.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# Ingest: footprint Edge.Cuts -> Component.required_cutouts  (#105)
# --------------------------------------------------------------------------
def test_footprint_edge_cuts_become_required_cutouts(tmp_path):
    from pcb_dfm.ingest.design_data import load_design_data

    dd = load_design_data(_write_board(tmp_path))
    j1 = next(c for c in dd.components if c.ref == "J1")
    assert len(j1.required_cutouts) == 1

    ring = j1.required_cutouts[0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    # Placed at (20, 15); Y is flipped into the artwork frame with pads/courtyard,
    # so the opening spans x 17..23 and y -17..-13.
    assert min(xs) == pytest.approx(17.0)
    assert max(xs) == pytest.approx(23.0)
    assert min(ys) == pytest.approx(-17.0)
    assert max(ys) == pytest.approx(-13.0)


def test_required_cutouts_follow_the_same_y_flip_as_pads(tmp_path):
    """A cutout that misses the flip lands mirrored about X -- and on a symmetric
    board it looks almost right, so this is pinned explicitly."""
    from pcb_dfm.ingest.design_data import load_design_data

    dd = load_design_data(_write_board(tmp_path))
    j1 = next(c for c in dd.components if c.ref == "J1")
    cy = sum(p[1] for p in j1.required_cutouts[0]) / len(j1.required_cutouts[0])
    assert cy == pytest.approx(j1.y_mm)      # same frame as the component origin
    assert cy < 0                            # flipped, not the raw KiCad +15


def test_a_rotated_footprint_rotates_its_cutout(tmp_path):
    from pcb_dfm.ingest.design_data import load_design_data

    rotated = _BOARD.replace('(at 20 15 0)', '(at 20 15 90)')
    dd = load_design_data(_write_board(tmp_path, rotated))
    ring = next(c for c in dd.components if c.ref == "J1").required_cutouts[0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    # 6 x 4 rotated 90 deg -> 4 wide, 6 tall.
    assert max(xs) - min(xs) == pytest.approx(4.0)
    assert max(ys) - min(ys) == pytest.approx(6.0)


def test_a_concave_cutout_is_not_convex_hulled(tmp_path):
    """An L-shaped opening must survive as drawn; a hull would claim milled area
    that is not milled."""
    from pcb_dfm.ingest.design_data import load_design_data

    board = """(kicad_pcb
  (footprint "X:L_Slot" (layer "F.Cu") (at 0 0 0)
    (property "Reference" "J9")
    (fp_poly (pts (xy 0 0) (xy 6 0) (xy 6 2) (xy 2 2) (xy 2 6) (xy 0 6))
             (layer "Edge.Cuts"))
  )
)
"""
    dd = load_design_data(_write_board(tmp_path, board))
    ring = next(c for c in dd.components if c.ref == "J9").required_cutouts[0]
    assert len(ring) == 6           # all six vertices, not a 4-corner hull
    from pcb_dfm.checks._design_advisory import point_inside
    # The notch corner is outside the L but inside its convex hull.
    assert not point_inside(ring, 5.0, -5.0)


def test_footprint_without_edge_cuts_declares_nothing(tmp_path):
    from pcb_dfm.ingest.design_data import load_design_data

    plain = _BOARD.replace('(layer "Edge.Cuts")', '(layer "F.SilkS")')
    dd = load_design_data(_write_board(tmp_path, plain))
    assert next(c for c in dd.components if c.ref == "J1").required_cutouts == []


# --------------------------------------------------------------------------
# Check harness (design-data only; outline comes from ctx.ingest)
# --------------------------------------------------------------------------
class _F:
    def __init__(self, path, layer_type):
        self.path = path
        self.layer_type = layer_type


class _Ingest:
    def __init__(self, files):
        self.files = files


def _outline_gerber(tmp_path: Path, boundary, cutouts=()) -> Path:
    """A minimal Gerber outline: one boundary contour plus optional cutouts."""
    lines = ["%FSLAX36Y36*%", "%MOMM*%", "%ADD10C,0.10*%", "D10*"]
    for ring in [boundary, *cutouts]:
        for i, (x, y) in enumerate(ring):
            op = "D02" if i == 0 else "D01"
            lines.append(f"X{int(round(x * 1e6))}Y{int(round(y * 1e6))}{op}*")
        x0, y0 = ring[0]
        lines.append(f"X{int(round(x0 * 1e6))}Y{int(round(y0 * 1e6))}D01*")
    lines.append("M02*")
    p = tmp_path / "outline.gko"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _run(check_id: str, dd, outline_path=None):
    _ensure_impls_loaded()
    ingest = _Ingest([_F(outline_path, "outline")] if outline_path else [])
    ctx = CheckContext(
        check_def=load_check_definition(check_id), ingest=ingest,
        geometry=BoardGeometry(root_dir=Path(".")), geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=dd)
    return get_check_runner(check_id)(ctx)


_BOUNDARY = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
_OPENING = [(17.0, 13.0), (23.0, 13.0), (23.0, 17.0), (17.0, 17.0)]


def _comp_with_cutout(ring: List[Tuple[float, float]] = None) -> DesignData:
    dd = DesignData(source="test")
    dd.components = [Component(ref="J1", footprint="USB_C_Mid_Mount",
                               x_mm=20.0, y_mm=15.0,
                               required_cutouts=[list(ring or _OPENING)])]
    return dd


# --------------------------------------------------------------------------
# component_cutout_present  (#105)
# --------------------------------------------------------------------------
def test_declared_cutout_present_passes(tmp_path):
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING])
    r = _run("component_cutout_present", _comp_with_cutout(), path)
    assert r.status == "pass"
    assert r.metric.measured_value == 0.0


def test_declared_cutout_missing_fails(tmp_path):
    path = _outline_gerber(tmp_path, _BOUNDARY)          # no cutout milled
    r = _run("component_cutout_present", _comp_with_cutout(), path)
    assert r.status == "fail"
    assert r.metric.measured_value == 1.0
    assert "J1" in r.violations[0].message


def test_cutout_milled_somewhere_else_still_fails(tmp_path):
    elsewhere = [(2.0, 2.0), (8.0, 2.0), (8.0, 6.0), (2.0, 6.0)]
    path = _outline_gerber(tmp_path, _BOUNDARY, [elsewhere])
    r = _run("component_cutout_present", _comp_with_cutout(), path)
    assert r.status == "fail"


def test_board_cutout_nobody_declared_is_not_a_finding(tmp_path):
    """Ventilation / clearance / mounting openings are legitimate. The check is
    one-directional by design."""
    vent = [(30.0, 20.0), (35.0, 20.0), (35.0, 25.0), (30.0, 25.0)]
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING, vent])
    r = _run("component_cutout_present", _comp_with_cutout(), path)
    assert r.status == "pass"


def test_no_declaring_footprint_is_not_applicable(tmp_path):
    dd = DesignData(source="test")
    dd.components = [Component(ref="R1", x_mm=5.0, y_mm=5.0)]
    r = _run("component_cutout_present", dd, _outline_gerber(tmp_path, _BOUNDARY))
    assert r.status == "not_applicable"


def test_no_design_data_is_not_applicable(tmp_path):
    r = _run("component_cutout_present", DesignData(source="test"),
             _outline_gerber(tmp_path, _BOUNDARY))
    assert r.status == "not_applicable"


# --------------------------------------------------------------------------
# pad_over_cutout  (#106)
# --------------------------------------------------------------------------
def _dd_with_pad(x: float, y: float, declared=False) -> DesignData:
    dd = DesignData(source="test")
    comp = Component(
        ref="U1", x_mm=x, y_mm=y,
        pads=[Pad(name="1", x_mm=x, y_mm=y, width_mm=1.0, height_mm=0.6)],
        required_cutouts=[list(_OPENING)] if declared else [],
    )
    dd.components = [comp]
    return dd


def test_pad_over_cutout_is_flagged(tmp_path):
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING])
    r = _run("pad_over_cutout", _dd_with_pad(20.0, 15.0), path)
    assert r.status == "warning"
    assert r.metric.measured_value == 1.0
    assert "U1.1" in r.violations[0].message


def test_pad_clear_of_the_cutout_passes(tmp_path):
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING])
    r = _run("pad_over_cutout", _dd_with_pad(5.0, 5.0), path)
    assert r.status == "pass"


def test_pad_in_its_own_declared_cutout_is_intentional(tmp_path):
    """The mid-mount case: without this exclusion every correctly designed
    mid-mount connector on the board is a finding."""
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING])
    r = _run("pad_over_cutout", _dd_with_pad(20.0, 15.0, declared=True), path)
    assert r.status == "pass"


def test_pad_partly_over_the_cutout_edge_is_flagged(tmp_path):
    """Overlap, not containment: a pad straddling the milled edge still has
    unsupported copper."""
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING])
    r = _run("pad_over_cutout", _dd_with_pad(17.0, 15.0), path)
    assert r.status == "warning"


def test_board_without_cutouts_is_not_applicable(tmp_path):
    path = _outline_gerber(tmp_path, _BOUNDARY)
    r = _run("pad_over_cutout", _dd_with_pad(20.0, 15.0), path)
    assert r.status == "not_applicable"


# --------------------------------------------------------------------------
# #107: tall_part_edge_clearance sees cutouts
# --------------------------------------------------------------------------
def _tall(x: float, y: float, declared=False) -> DesignData:
    dd = DesignData(source="test")
    dd.components = [Component(ref="C9", x_mm=x, y_mm=y, height_mm=8.0,
                               required_cutouts=[list(_OPENING)] if declared else [])]
    return dd


def test_tall_part_at_a_cutout_lip_is_flagged(tmp_path):
    """The blind spot: measured against the boundary alone this part is 13 mm
    from any edge and perfectly clean."""
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING])
    r = _run("tall_part_edge_clearance", _tall(16.0, 15.0), path)
    assert r.status == "warning"
    assert "C9" in r.violations[0].message


def test_tall_part_away_from_every_edge_passes(tmp_path):
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING])
    r = _run("tall_part_edge_clearance", _tall(8.0, 8.0), path)
    assert r.status == "pass"


def test_tall_part_seated_in_its_own_cutout_is_not_crowding(tmp_path):
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING])
    r = _run("tall_part_edge_clearance", _tall(20.0, 15.0, declared=True), path)
    assert r.status == "pass"


def test_tall_part_near_the_outer_boundary_still_flagged(tmp_path):
    """The pre-existing behaviour must survive the cutout change."""
    path = _outline_gerber(tmp_path, _BOUNDARY, [_OPENING])
    r = _run("tall_part_edge_clearance", _tall(1.0, 15.0), path)
    assert r.status == "warning"


# --------------------------------------------------------------------------
# Scope of component_cutout_present: what it can and cannot detect.
#
# These pin the empirical finding that rescoped the check. KiCad plots a
# footprint's Edge.Cuts graphics INTO the board outline layer, so there is no
# "propagate the cutout" step to forget: when design data and artwork come from
# one board file the check passes trivially. Its real subject is a fab package
# out of sync with the board it is paired with.
# --------------------------------------------------------------------------
def test_declared_cutout_is_satisfied_when_artwork_comes_from_the_same_design():
    """Same-source is the common case and must never be a finding.

    Modelled directly: the outline carries the opening the footprint declares,
    which is what KiCad's own plot produces.
    """
    dd = _comp_with_cutout()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = _outline_gerber(Path(td), _BOUNDARY, [_OPENING])
        assert _run("component_cutout_present", dd, path).status == "pass"


def test_stale_artwork_against_an_updated_design_fails():
    """The failure this check actually exists for: the package predates the board.

    The design declares an opening; the artwork it was paired with does not have
    it, so the fab would mill the old outline. Both halves are internally valid,
    which is why nothing else catches it.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        stale = _outline_gerber(Path(td), _BOUNDARY)   # exported before the change
        r = _run("component_cutout_present", _comp_with_cutout(), stale)
    assert r.status == "fail"
    assert "J1" in r.violations[0].message


def test_a_footprint_that_declares_nothing_is_never_a_finding():
    """The limit of the check, stated as a test.

    A reverse-mount LED whose library footprint never drew its light window
    declares no cutout, so there is nothing to compare and nothing to report --
    catching that needs part knowledge, not geometry (docs §4/§5).
    """
    dd = DesignData(source="test")
    dd.components = [Component(ref="D1", footprint="droyd:LED_SK6812MINI-E",
                               x_mm=20.0, y_mm=15.0)]
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = _outline_gerber(Path(td), _BOUNDARY)    # no opening anywhere
        r = _run("component_cutout_present", dd, path)
    assert r.status == "not_applicable"
