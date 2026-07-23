"""
Per-check CORRECTNESS tests.

Each test builds a tiny board engineered to trip exactly ONE DFM check and
asserts BOTH the resulting status AND the measured metric value -- not merely
that the pipeline does not crash.

Gerber (RS-274X) and Excellon inputs are synthesized at RUNTIME from inline
strings into pytest's tmp_path and zipped there; no binaries are committed and
we do not rely on .gitignore.

Format notes:
  * RS-274X headers use %FSLAX46Y46*% + %MOMM*%, i.e. units mm, format 4.6, so
    integer coordinate tokens are millimetres * 1e6 (e.g. 5 mm -> 5000000).
  * Excellon files use METRIC decimal coordinates; tool diameters are mm.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks.definitions import load_check_definition
from pcb_dfm.engine.check_runner import run_single_check


# --------------------------------------------------------------------------
# Helper: turn {filename: content} into a zip Path inside tmp_path.
# --------------------------------------------------------------------------
def make_gerber_zip(tmp_path: Path, files: dict[str, str], name: str = "board.zip") -> Path:
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    return zip_path


# --------------------------------------------------------------------------
# Reusable inline artwork fragments.
# --------------------------------------------------------------------------
def _copper_trace(width_mm: float) -> str:
    """A single copper trace segment 8 mm long, drawn with a round aperture
    whose diameter IS the trace width."""
    return (
        "%FSLAX46Y46*%\n"
        "%MOMM*%\n"
        f"%ADD10C,{width_mm:.6f}*%\n"
        "D10*\n"
        "X1000000Y1000000D02*\n"
        "X9000000Y1000000D01*\n"
        "M02*\n"
    )


def _copper_rect_pad(cx_mm: float, cy_mm: float, w_mm: float, h_mm: float) -> str:
    """A single flashed rectangular copper pad."""
    return (
        "%FSLAX46Y46*%\n"
        "%MOMM*%\n"
        f"%ADD10R,{w_mm:.6f}X{h_mm:.6f}*%\n"
        "D10*\n"
        f"X{int(round(cx_mm * 1e6))}Y{int(round(cy_mm * 1e6))}D03*\n"
        "M02*\n"
    )


def _outline_rect(w_mm: float, h_mm: float) -> str:
    """A rectangular board outline drawn with a very thin (0.01 mm) aperture so
    the outline bounding box is essentially the drawn rectangle."""
    return (
        "%FSLAX46Y46*%\n"
        "%MOMM*%\n"
        "%ADD10C,0.010000*%\n"
        "D10*\n"
        "X0Y0D02*\n"
        f"X{int(round(w_mm * 1e6))}Y0D01*\n"
        f"X{int(round(w_mm * 1e6))}Y{int(round(h_mm * 1e6))}D01*\n"
        f"X0Y{int(round(h_mm * 1e6))}D01*\n"
        "X0Y0D01*\n"
        "M02*\n"
    )


def _drill(diameter_mm: float) -> str:
    """An Excellon file with a single plated hole of the given diameter."""
    return (
        "M48\n"
        "METRIC,TZ\n"
        f"T1C{diameter_mm:.3f}\n"
        "%\n"
        "T1\n"
        "X3.0Y8.0\n"
        "T0\n"
        "M30\n"
    )


def _drill_slot(diameter_mm: float) -> str:
    """An Excellon file with a single G85 routed slot; slot WIDTH == tool
    diameter, slot LENGTH == distance between the two G85 coordinates."""
    return (
        "M48\n"
        "METRIC,TZ\n"
        f"T1C{diameter_mm:.3f}\n"
        "%\n"
        "T1\n"
        "X3.0Y8.0G85X6.0Y8.0\n"
        "T0\n"
        "M30\n"
    )


# ==========================================================================
# 1 + 2. min_trace_width
# ==========================================================================
def test_min_trace_width_fail_thin_trace(tmp_path):
    # A 0.05 mm trace is below the absolute minimum (0.075 mm) -> fail.
    z = make_gerber_zip(tmp_path, {"board.gtl": _copper_trace(0.05)})
    result = run_single_check(z, load_check_definition("min_trace_width"))
    assert result.status == "fail"
    assert result.metric.measured_value == pytest.approx(0.05, abs=1e-3)


def test_min_trace_width_pass_wide_trace(tmp_path):
    # A comfortably wide 0.30 mm trace clears the recommended min (0.10 mm).
    z = make_gerber_zip(tmp_path, {"board.gtl": _copper_trace(0.30)})
    result = run_single_check(z, load_check_definition("min_trace_width"))
    assert result.status == "pass"
    assert result.metric.measured_value == pytest.approx(0.30, abs=1e-3)


# ==========================================================================
# 3. min_drill_size
# ==========================================================================
def test_min_drill_size_fail_small_hole(tmp_path):
    # impl absolute_min defaults to 0.15 mm; a 0.10 mm hole is below it -> fail.
    # (0.15 mm itself lands on the warning boundary because the impl uses a
    # strict '<', so we drive a value clearly under the absolute minimum.)
    z = make_gerber_zip(tmp_path, {"board.drl": _drill(0.10)})
    result = run_single_check(z, load_check_definition("min_drill_size"))
    assert result.status == "fail"
    assert result.metric.measured_value == pytest.approx(0.10, abs=1e-3)


# ==========================================================================
# 4. drill_aspect_ratio -- assert the metric is a dimensionless ratio (":1").
# ==========================================================================
def test_drill_aspect_ratio_units_are_ratio(tmp_path):
    # board_thickness default 1.6 mm / 0.30 mm hole = 5.333:1 (a passing ratio).
    z = make_gerber_zip(tmp_path, {"board.drl": _drill(0.30)})
    result = run_single_check(z, load_check_definition("drill_aspect_ratio"))
    # The important correctness property: the ratio is reported as ":1", never "%".
    assert result.metric.units == ":1"
    assert result.metric.units != "%"
    assert result.metric.measured_value == pytest.approx(1.6 / 0.30, abs=1e-2)
    assert result.status == "pass"


# ==========================================================================
# 5. min_slot_width -- a real routed slot narrower than the limit.
# ==========================================================================
def test_min_slot_width_fail_narrow_slot(tmp_path):
    # G85 routed slot, width == tool diameter 0.50 mm, below the 0.60 mm limit.
    z = make_gerber_zip(tmp_path, {"board.drl": _drill_slot(0.50)})
    result = run_single_check(z, load_check_definition("min_slot_width"))
    assert result.status == "fail"
    assert result.metric.measured_value == pytest.approx(0.50, abs=1e-3)


# ==========================================================================
# 6. copper_to_edge_distance -- copper flashed close to the board edge.
# ==========================================================================
def test_copper_to_edge_distance_fail(tmp_path):
    # 10x10 mm board; a 0.5 mm square copper pad centred at x=0.35 has its left
    # edge at x=0.10. Board min_x is ~-0.005 (half the 0.01 mm outline aperture),
    # so the measured copper-to-edge distance is ~0.105 mm -- below the 0.15 mm
    # absolute minimum -> fail.
    files = {
        "board.gtl": _copper_rect_pad(cx_mm=0.35, cy_mm=5.0, w_mm=0.5, h_mm=0.5),
        "board.gko": _outline_rect(10.0, 10.0),
    }
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("copper_to_edge_distance"))
    assert result.status == "fail"
    # Small distance that matches the engineered geometry (~0.105 mm).
    assert result.metric.measured_value == pytest.approx(0.105, abs=0.02)
    assert result.metric.measured_value < 0.15


# ==========================================================================
# 7. impedance_control -- needs a design-data sidecar (stackup + net).
# ==========================================================================
def test_impedance_control_fail_with_design_data(tmp_path):
    from pcb_dfm.checks.impl_impedance_control import _microstrip_z0

    # Bare gerber (content irrelevant; impedance is computed from design_data).
    z = make_gerber_zip(tmp_path, {"board.gtl": _copper_trace(0.20)})

    er, h_mm, w_mm, t_mm = 4.2, 0.20, 0.20, 0.035
    target_ohm = 50.0
    expected_z0 = _microstrip_z0(er, h_mm, w_mm, t_mm)
    expected_dev = abs(expected_z0 - target_ohm) / target_ohm * 100.0  # ~33%

    design_data = {
        "stackup": {
            "er": er,
            "dielectric_thickness_mm": h_mm,
            "copper_thickness_mm": t_mm,
        },
        "controlled_impedance": [
            {"name": "CLK", "width_mm": w_mm, "target_ohm": target_ohm},
        ],
    }

    result = run_single_check(
        z, load_check_definition("impedance_control"), design_data=design_data
    )
    # ~33% deviation is far past the 10% limit -> fail.
    assert result.status == "fail"
    assert result.metric.units == "%"
    assert result.metric.measured_value == pytest.approx(expected_dev, rel=1e-6)


def test_impedance_control_not_applicable_without_design_data(tmp_path):
    z = make_gerber_zip(tmp_path, {"board.gtl": _copper_trace(0.20)})
    result = run_single_check(z, load_check_definition("impedance_control"))
    # Impedance cannot be validated from bare artwork -> not_applicable.
    assert result.status == "not_applicable"
    assert result.metric.measured_value is None


# ==========================================================================
# 8. solder_paste_area_coverage -- paste aperture over a copper pad.
# ==========================================================================
def test_solder_paste_area_coverage_ratio(tmp_path):
    # Copper pad 1.0x1.0 mm (area 1.0 mm^2), paste aperture 0.8x0.8 mm
    # (area 0.64 mm^2), co-located -> coverage = 0.64 / 1.0 = 64%.
    files = {
        "board.gtl": _copper_rect_pad(cx_mm=5.0, cy_mm=5.0, w_mm=1.0, h_mm=1.0),
        "board.gtp": _copper_rect_pad(cx_mm=5.0, cy_mm=5.0, w_mm=0.8, h_mm=0.8),
    }
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("solder_paste_area_coverage"))
    assert result.metric.units == "%"
    assert result.metric.measured_value == pytest.approx(64.0, abs=0.5)
    # 64% sits inside the recommended 50-120% range -> pass.
    assert result.status == "pass"
