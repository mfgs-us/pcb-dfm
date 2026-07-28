"""schematic_layout_consistency: NC-aware footprint/pin-mapping check."""

from __future__ import annotations

from pathlib import Path

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import Component, DesignData, Pad


def _dd(pads, pin_types, nc_pins=None):
    dd = DesignData(source="test")
    dd.components = [Component(ref="U1", value="X",
                              pads=[Pad(name=n, x_mm=float(i), y_mm=0.0)
                                    for i, n in enumerate(pads)])]
    dd.pin_types = pin_types
    dd.nc_pins = nc_pins or set()
    return dd


def run(dd):
    _ensure_impls_loaded()
    ctx = CheckContext(
        check_def=load_check_definition("schematic_layout_consistency"), ingest=None,
        geometry=BoardGeometry(root_dir=Path(".")), geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="t", gerber_zip=Path("x"), design_data=dd)
    return get_check_runner("schematic_layout_consistency")(ctx)


def test_missing_pad_flagged():
    # Schematic has pins 1,2,3; footprint only 1,2; pin 3 is not NC -> error.
    dd = _dd(["1", "2"], {("U1", "1"): "input", ("U1", "2"): "input", ("U1", "3"): "input"})
    r = run(dd)
    assert r.status == "warning" and "U1" in r.violations[0].message


def test_nc_pin_excluded():
    dd = _dd(["1", "2"], {("U1", "1"): "input", ("U1", "2"): "input", ("U1", "3"): "input"},
             nc_pins={("U1", "3")})
    assert run(dd).status == "pass"


def test_mechanical_pin_excluded():
    # A non-numeric mechanical pin ("MP") with no pad is allowed.
    dd = _dd(["1", "2"], {("U1", "1"): "input", ("U1", "2"): "input", ("U1", "MP"): "passive"})
    assert run(dd).status == "pass"


def test_all_pins_present_passes():
    dd = _dd(["1", "2", "3"], {("U1", "1"): "input", ("U1", "2"): "input", ("U1", "3"): "input"})
    assert run(dd).status == "pass"


def test_na_without_schematic():
    dd = _dd(["1", "2"], {})
    assert run(dd).status == "not_applicable"
