"""Schematic ingest: pin electrical types + net-function refinement."""

from __future__ import annotations

from pcb_dfm.ingest.adapters.kicad import _parse_schematic_pin_types, _parse_sexpr
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


def test_parse_schematic_pin_types():
    pt = _parse_schematic_pin_types(_parse_sexpr(_SCH))
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
