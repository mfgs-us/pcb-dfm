"""Schematic ingest: pin electrical types + net-function refinement."""

from __future__ import annotations

from pcb_dfm.ingest.adapters.kicad import (
    _parse_schematic_functional,
    _parse_sexpr,
    _read_schematic_functional,
)
from pcb_dfm.ingest.design_intel import net_function_with_pins

_SCH = """
(kicad_sch
  (lib_symbols
    (symbol "Battery:BQ"
      (symbol "BQ_0_1"
        (pin power_in line (name "BAT_NEG" (effects)) (number "2" (effects))))
      (symbol "BQ_1_1"
        (pin open_collector line (name "STAT" (effects)) (number "3" (effects)))))
    (symbol "power:GND"
      (symbol "GND_0_1"
        (pin power_in line (name "GND" (effects)) (number "1" (effects))))))
  (symbol (lib_id "Battery:BQ")
    (property "Reference" "U3" (at 0 0 0))
    (property "Value" "BQ24075" (at 0 0 0)))
  (symbol (lib_id "power:GND")
    (property "Reference" "#PWR01" (at 0 0 0))
    (property "Value" "GND" (at 0 0 0))))
"""


def test_parse_schematic_functional():
    pt, _nc = _parse_schematic_functional(_parse_sexpr(_SCH))
    assert pt[("U3", "2")] == "power_in"
    assert pt[("U3", "3")] == "open_collector"
    # The #PWR power symbol has no footprint -> excluded.
    assert not any(ref.startswith("#") for (ref, _pin) in pt)


# -- net_function_with_pins -------------------------------------------------
def test_power_pin_promotes_unnamed_rail():
    # A net a name can't classify, but it feeds a power pin -> it's a rail.
    assert net_function_with_pins("BAT_NEG", None, {"power_in"}) == "ground"
    assert net_function_with_pins("VSYS_SW", None, {"power_in"}) == "power"


def test_name_classification_still_wins():
    assert net_function_with_pins("GND", None, set()) == "ground"
    assert net_function_with_pins("+3V3", None, set()) == "power"


def test_signal_stays_signal_without_power_pin():
    assert net_function_with_pins("USB_DP", None, {"bidirectional"}) == "signal"
    assert net_function_with_pins("SCK", None, {"output"}) == "signal"


# -- no-connect marker matching --------------------------------------------
_SCH_NC = """
(kicad_sch
  (lib_symbols
    (symbol "Lib:Part"
      (symbol "Part_1_1"
        (pin input line (at 0 5.08 270) (name "A" (effects)) (number "1" (effects)))
        (pin input line (at 0 -5.08 90) (name "B" (effects)) (number "2" (effects))))))
  (symbol (lib_id "Lib:Part") (at 100 100 0) (unit 1)
    (property "Reference" "U9" (at 0 0 0)) (property "Value" "X" (at 0 0 0)))
  (no_connect (at 100 94.92) (uuid "aaa")))
"""


def test_no_connect_matched_to_pin():
    # Pin 1 lib (0, 5.08) on an instance at (100,100,rot0): Y-flip -> (100, 94.92),
    # exactly the no_connect marker -> pin 1 is NC; pin 2 (at (100,105.08)) is not.
    _pt, nc = _parse_schematic_functional(_parse_sexpr(_SCH_NC))
    assert ("U9", "1") in nc
    assert ("U9", "2") not in nc


# -- hierarchical sheet walk (#86) -----------------------------------------
# A root sheet with one root-level part plus a (sheet ...) pointing at a
# sub-sheet that carries its own part and a no-connect. Before #86 only the root
# was parsed, so U2's pin types and NC were missed entirely.
_ROOT_SCH = """
(kicad_sch
  (lib_symbols
    (symbol "MCU:U"
      (symbol "U_1_1"
        (pin bidirectional line (at 0 0 0) (name "IO" (effects)) (number "1" (effects))))))
  (symbol (lib_id "MCU:U") (at 50 50 0) (unit 1)
    (property "Reference" "U1" (at 0 0 0)) (property "Value" "MCU" (at 0 0 0)))
  (sheet (at 100 20 0)
    (property "Sheetname" "Sub" (at 0 0 0))
    (property "Sheetfile" "sub.kicad_sch" (at 0 0 0))))
"""

_SUB_SCH = """
(kicad_sch
  (lib_symbols
    (symbol "Reg:LDO"
      (symbol "LDO_1_1"
        (pin input line (at 0 5.08 270) (name "EN" (effects)) (number "1" (effects)))
        (pin power_out line (at 0 -5.08 90) (name "VO" (effects)) (number "2" (effects))))))
  (symbol (lib_id "Reg:LDO") (at 100 100 0) (unit 1)
    (property "Reference" "U2" (at 0 0 0)) (property "Value" "LDO" (at 0 0 0)))
  (no_connect (at 100 94.92) (uuid "n1")))
"""


def _write_hier(tmp_path, root=_ROOT_SCH, sub=_SUB_SCH):
    (tmp_path / "top.kicad_sch").write_text(root, encoding="utf-8")
    if sub is not None:
        (tmp_path / "sub.kicad_sch").write_text(sub, encoding="utf-8")
    return tmp_path / "top.kicad_pcb"  # sibling the reader derives .kicad_sch from


def test_hierarchical_walk_collects_subsheet(tmp_path):
    board = _write_hier(tmp_path)
    pt, nc = _read_schematic_functional(board)
    # Root part still parsed.
    assert pt[("U1", "1")] == "bidirectional"
    # Sub-sheet part now covered (the whole point of #86).
    assert pt[("U2", "1")] == "input"
    assert pt[("U2", "2")] == "power_out"
    # Sub-sheet no-connect matched in the sub-sheet's own coordinate frame.
    assert ("U2", "1") in nc
    assert ("U2", "2") not in nc


def test_missing_subsheet_file_is_ignored(tmp_path):
    # Root references a sub-sheet that isn't on disk -> root still parses, no crash.
    board = _write_hier(tmp_path, sub=None)
    pt, _nc = _read_schematic_functional(board)
    assert pt[("U1", "1")] == "bidirectional"
    assert not any(ref == "U2" for (ref, _p) in pt)


def test_sheet_cycle_terminates(tmp_path):
    # A sub-sheet that points back at the root must not loop forever.
    sub_cyclic = _SUB_SCH.rstrip()[:-1] + """
  (sheet (at 0 0 0)
    (property "Sheetname" "Back" (at 0 0 0))
    (property "Sheetfile" "top.kicad_sch" (at 0 0 0))))
"""
    board = _write_hier(tmp_path, sub=sub_cyclic)
    pt, nc = _read_schematic_functional(board)  # returns rather than hangs
    assert pt[("U1", "1")] == "bidirectional"
    assert pt[("U2", "2")] == "power_out"
