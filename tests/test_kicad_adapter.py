"""KiCad project -> DesignData adapter (Tier 1: design-data only)."""

from __future__ import annotations

import math
import pathlib

from pcb_dfm.ingest.design_data import load_design_data

# A minimal but realistic KiCad 6/7-style board: 2-copper stackup, four nets,
# a net class, a routed diff pair, and two placed footprints.
_BOARD = """
(kicad_pcb (version 20221018) (generator pcbnew)
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup
    (stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4") (epsilon_r 4.5))
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "USB_DP")
  (net 3 "USB_DN")
  (net_class "HighSpeed" "USB diff pair"
    (clearance 0.2) (trace_width 0.25) (diff_pair_width 0.2) (diff_pair_gap 0.2)
    (add_net "USB_DP") (add_net "USB_DN"))
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu")
    (at 100 50 90)
    (property "Reference" "R1")
    (property "Value" "10k"))
  (footprint "Capacitor_SMD:C_0402" (layer "B.Cu")
    (at 120 60 0)
    (property "Reference" "C1")
    (property "Value" "100nF"))
  (segment (start 100 50) (end 110 50) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 100 50.3) (end 110 50.3) (width 0.2) (layer "F.Cu") (net 3))
)
"""


def _write_project(tmp_path) -> pathlib.Path:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "proj.kicad_pcb").write_text(_BOARD, encoding="utf-8")
    return d


def test_loads_from_project_dir(tmp_path):
    dd = load_design_data(_write_project(tmp_path))
    assert dd is not None
    assert dd.source == "kicad"


def test_stackup_copper_and_dielectric(tmp_path):
    dd = load_design_data(_write_project(tmp_path))
    su = dd.stackup
    assert su is not None
    # Silk/mask layers are excluded; only copper + dielectric remain.
    assert len(su.copper_layers()) == 2
    assert len(su.dielectric_layers()) == 1
    assert su.er == 4.5
    assert math.isclose(su.total_thickness_mm(), 1.58, abs_tol=1e-9)


def test_nets_routes_and_netclass(tmp_path):
    dd = load_design_data(_write_project(tmp_path))
    # Net 0 (unconnected) is dropped; the three named nets remain.
    assert set(dd.nets) == {"GND", "USB_DP", "USB_DN"}
    dp = dd.net("USB_DP")
    assert dp is not None
    assert dp.net_class == "HighSpeed"           # from the board net_class block
    assert math.isclose(dp.routed_length_mm(), 10.0, abs_tol=1e-9)
    assert dp.has_geometry()
    (_seg, layer, width), = dp.route_segments()
    assert layer == "F.Cu" and math.isclose(width, 0.2, abs_tol=1e-9)


def test_pads_parsed_with_absolute_positions(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "p.kicad_pcb").write_text(
        '(kicad_pcb (net 0 "")\n'
        '  (footprint "Diode_SMD:D_0603" (layer "F.Cu") (at 100 50 0)\n'
        '    (property "Reference" "D1")\n'
        '    (pad "1" smd rect (at -0.75 0) (size 0.9 0.8) (layers "F.Cu"))\n'
        '    (pad "2" smd rect (at 0.75 0) (size 0.9 0.8) (layers "F.Cu")))\n'
        '  (footprint "Connector:PinHeader" (layer "F.Cu") (at 10 10 0)\n'
        '    (property "Reference" "J1")\n'
        '    (pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu"))))',
        encoding="utf-8",
    )
    dd = load_design_data(d)
    comps = {c.ref: c for c in dd.components}
    d1 = comps["D1"]
    assert len(d1.pads) == 2
    p1 = d1.pin1()
    assert p1 is not None and p1.x_mm == 99.25 and p1.y_mm == 50.0   # 100 + (-0.75)
    assert not p1.through_hole
    assert comps["J1"].pads[0].through_hole is True


def test_pad_positions_rotate_with_footprint(tmp_path):
    d = tmp_path / "pr"
    d.mkdir()
    (d / "pr.kicad_pcb").write_text(
        '(kicad_pcb (net 0 "")\n'
        '  (footprint "Diode_SMD:D_0603" (layer "F.Cu") (at 100 50 90)\n'
        '    (property "Reference" "D1")\n'
        '    (pad "1" smd rect (at -0.75 0) (size 0.9 0.8) (layers "F.Cu"))))',
        encoding="utf-8",
    )
    p1 = {c.ref: c for c in load_design_data(d).components}["D1"].pin1()
    # (-0.75, 0) rotated 90° -> (0, -0.75); abs = (100, 49.25)
    assert abs(p1.x_mm - 100.0) < 1e-9 and abs(p1.y_mm - 49.25) < 1e-9


def test_vias_parsed_onto_net(tmp_path):
    d = tmp_path / "v"
    d.mkdir()
    (d / "v.kicad_pcb").write_text(
        '(kicad_pcb (net 0 "") (net 1 "CLK")\n'
        '  (segment (start 0 0) (end 5 0) (width 0.2) (layer "F.Cu") (net 1))\n'
        '  (via (at 5 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)))',
        encoding="utf-8",
    )
    dd = load_design_data(d)
    clk = dd.net("CLK")
    assert clk is not None and len(clk.vias) == 1
    assert clk.vias[0].x_mm == 5.0 and clk.vias[0].to_layer == "B.Cu"


def test_diff_pair_inferred(tmp_path):
    dd = load_design_data(_write_project(tmp_path))
    pairs = {(p.positive, p.negative) for p in dd.diff_pairs}
    assert ("USB_DP", "USB_DN") in pairs


def test_components_placement(tmp_path):
    dd = load_design_data(_write_project(tmp_path))
    comps = {c.ref: c for c in dd.components}
    assert set(comps) == {"R1", "C1"}
    r1 = comps["R1"]
    assert r1.value == "10k" and r1.side == "top"
    assert r1.x_mm == 100.0 and r1.y_mm == 50.0 and r1.rotation_deg == 90.0
    assert comps["C1"].side == "bottom"


def test_project_netclass_patterns(tmp_path):
    """KiCad 7 stores net classes in the .kicad_pro; assign by glob pattern."""
    d = tmp_path / "p7"
    d.mkdir()
    (d / "p7.kicad_pcb").write_text(
        '(kicad_pcb (net 0 "") (net 1 "DDR_A0") (net 2 "DDR_A1") (net 3 "SPI_CLK"))',
        encoding="utf-8",
    )
    (d / "p7.kicad_pro").write_text(
        '{"net_settings": {"netclass_patterns": [{"netclass": "DDR", "pattern": "DDR_*"}]}}',
        encoding="utf-8",
    )
    dd = load_design_data(d)
    assert dd.net("DDR_A0").net_class == "DDR"
    assert dd.net("DDR_A1").net_class == "DDR"
    assert dd.net("SPI_CLK").net_class is None
