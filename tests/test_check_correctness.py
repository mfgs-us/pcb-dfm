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


def _drill_holes(diameter_mm: float, positions: list[tuple[float, float]]) -> str:
    """An Excellon file with several plated holes of one diameter at ``positions``."""
    lines = ["M48", "METRIC,TZ", f"T1C{diameter_mm:.3f}", "%", "T1"]
    lines += [f"X{x:.3f}Y{y:.3f}" for (x, y) in positions]
    lines += ["T0", "M30", ""]
    return "\n".join(lines)


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


# ==========================================================================
# 8b. stencil_aperture_ratio -- IPC-7525 paste-release area ratio.
#     For a square aperture side s over a foil of thickness t, the area ratio
#     is s^2 / (4 s t) = s / (4 t); with the default t = 0.12 mm this is s/0.48.
# ==========================================================================
def test_stencil_aperture_ratio_pass_large_aperture(tmp_path):
    # 0.6 mm square -> AR = 0.6 / 0.48 = 1.25, well above the 0.66 floor.
    files = {"board.gtp": _copper_rect_pad(cx_mm=5.0, cy_mm=5.0, w_mm=0.6, h_mm=0.6)}
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("stencil_aperture_ratio"))
    assert result.metric.measured_value == pytest.approx(1.25, abs=0.03)
    assert result.status == "pass"


def test_stencil_aperture_ratio_warns_marginal_aperture(tmp_path):
    # 0.28 mm square -> AR = 0.28 / 0.48 = 0.583, between the 0.5 and 0.66 lines.
    files = {"board.gtp": _copper_rect_pad(cx_mm=5.0, cy_mm=5.0, w_mm=0.28, h_mm=0.28)}
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("stencil_aperture_ratio"))
    assert result.metric.measured_value == pytest.approx(0.583, abs=0.03)
    assert result.status == "warning"


def test_stencil_aperture_ratio_assumed_foil_caps_at_warning(tmp_path):
    # 0.2 mm square -> AR = 0.417 (< 0.5). With no stencil_thickness_mm supplied
    # the foil is assumed, so an un-releasable aperture is capped at a warning
    # rather than hard-failing on an assumption.
    files = {"board.gtp": _copper_rect_pad(cx_mm=5.0, cy_mm=5.0, w_mm=0.2, h_mm=0.2)}
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("stencil_aperture_ratio"))
    assert result.metric.measured_value == pytest.approx(0.417, abs=0.03)
    assert result.status == "warning"


def test_stencil_aperture_ratio_authoritative_thickness_fails(tmp_path):
    # Same 0.2 mm aperture, but now the design-data states the foil thickness ->
    # authoritative -> AR 0.417 (< 0.5) hard-fails.
    from pcb_dfm.ingest.design_model import DesignData
    files = {"board.gtp": _copper_rect_pad(cx_mm=5.0, cy_mm=5.0, w_mm=0.2, h_mm=0.2)}
    z = make_gerber_zip(tmp_path, files)
    dd = DesignData(source="test", stencil_thickness_mm=0.12)
    result = run_single_check(
        z, load_check_definition("stencil_aperture_ratio"), design_data=dd)
    assert result.metric.measured_value == pytest.approx(0.417, abs=0.03)
    assert result.status == "fail"


def test_stencil_aperture_ratio_not_applicable_without_paste(tmp_path):
    files = {"board.gtl": _copper_rect_pad(cx_mm=5.0, cy_mm=5.0, w_mm=1.0, h_mm=1.0)}
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("stencil_aperture_ratio"))
    assert result.status == "not_applicable"


# ==========================================================================
# 8c. castellated_edge_plating -- plated holes crossing the board outline.
# ==========================================================================
def test_castellated_edge_plating_pass_bisected_hole(tmp_path):
    # A 0.6 mm plated hole centred ON the right edge (x=10) -> cleanly bisected.
    files = {
        "board.gko": _outline_rect(10.0, 10.0),
        "board.drl": _drill_holes(0.6, [(10.0, 5.0)]),
    }
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("castellated_edge_plating"))
    assert result.status == "pass"
    assert result.metric.measured_value == 0.0


def test_castellated_edge_plating_warns_sliver(tmp_path):
    # 0.6 mm hole centred 0.2 mm OUTSIDE the right edge -> < half the barrel
    # remains in copper -> plating sliver.
    files = {
        "board.gko": _outline_rect(10.0, 10.0),
        "board.drl": _drill_holes(0.6, [(10.2, 5.0)]),
    }
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("castellated_edge_plating"))
    assert result.status == "warning"
    assert result.metric.measured_value == 1.0


def test_castellated_edge_plating_warns_tight_pitch(tmp_path):
    # Two bisected castellations 0.5 mm centre-to-centre (< 1.0 mm floor).
    files = {
        "board.gko": _outline_rect(10.0, 10.0),
        "board.drl": _drill_holes(0.5, [(10.0, 5.0), (10.0, 5.5)]),
    }
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("castellated_edge_plating"))
    assert result.status == "warning"
    assert result.metric.measured_value == 1.0


def test_castellated_edge_plating_not_applicable_internal_hole(tmp_path):
    # An ordinary plated hole in the middle of the board crosses no edge.
    files = {
        "board.gko": _outline_rect(10.0, 10.0),
        "board.drl": _drill_holes(0.6, [(5.0, 5.0)]),
    }
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("castellated_edge_plating"))
    assert result.status == "not_applicable"


def test_solder_mask_web_catches_thin_web_between_large_openings(tmp_path):
    """Regression: the web pairing must be size-independent.

    The check used to index each mask opening as a point at its centroid and
    pair openings only within a fixed ~0.75 mm cell block. Two ordinary 1.2 mm
    pads have centroids ~1.24 mm apart, so a razor-thin mask web between them was
    never paired and silently passed with measured=None -- a solder-bridging
    escape on exactly the boards it matters for. The web must now be measured.

    The outcome is a warning, not a fail: a thin web is advisory without net data
    to confirm the adjacent openings are different nets (see the impl). The point
    this pins is that the thin web is DETECTED (measured ~0.04 mm) rather than
    silently passed.
    """
    # Openings centred at x=5.0 and x=6.24 -> gap 1.24 mm between centres, so a
    # 1.2 mm-wide opening leaves a 0.04 mm web.
    mask = (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD11R,1.200000X1.200000*%\nD11*\n"
        "X5000000Y5000000D03*\nX6240000Y5000000D03*\nM02*\n"
    )
    files = {
        "board.gts": mask,
        "board.gko": _outline_rect(12.0, 12.0),
    }
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("solder_mask_web"))
    # Detected (not the old measured=None pass) and flagged as an advisory warning.
    assert result.metric.measured_value == pytest.approx(0.04, abs=0.005)
    assert result.status == "warning"


def test_silkscreen_min_width_detects_thin_long_line(tmp_path):
    """Regression: a too-thin silk line must not be dropped by an aspect cutoff.

    A too-thin silk stroke is inherently long-and-thin (high aspect). The check
    used to skip any feature with aspect > 30, so a 4 mm x 0.05 mm line (aspect
    80) was dropped and the board passed with measured=None. It must now be
    measured. The outcome is an advisory warning (silk width is a legibility, not
    a functional, concern) -- the point is that the thin line is DETECTED.
    """
    # A 4 mm silk line drawn with a 0.05 mm round aperture (below the 0.08 mm
    # absolute minimum), well within the board.
    silk = (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD20C,0.050000*%\nD20*\n"
        "X2000000Y6000000D02*\nX6000000Y6000000D01*\nM02*\n"
    )
    files = {
        "board.gto": silk,
        "board.gko": _outline_rect(12.0, 12.0),
    }
    z = make_gerber_zip(tmp_path, files)
    result = run_single_check(z, load_check_definition("silkscreen_min_width"))
    assert result.metric.measured_value == pytest.approx(0.05, abs=0.005)
    assert result.status == "warning"


def _one_aperture_board(ap_def: str) -> str:
    """A one-flash Gerber whose only aperture is `ap_def` (e.g. 'C,0.002')."""
    return (
        "%FSLAX46Y46*%\n%MOMM*%\n"
        f"%ADD10{ap_def}*%\nD10*\n"
        "X1000000Y1000000D03*\nM02*\n"
    )


def test_aperture_definition_errors_flags_out_of_range_sizes(tmp_path):
    """Regression: the check must flag implausibly small/large apertures.

    Its HARD_REASONS had drifted from the reason strings the validator emits
    ("extremely_small" vs "too_small"), so no size-based violation ever counted
    and the check could not fail on any out-of-range aperture -- its headline
    purpose. A 0.002 mm aperture (below the 0.01 mm floor) must now be flagged; a
    normal 0.25 mm aperture must not.
    """
    cd = load_check_definition("aperture_definition_errors")

    tiny = run_single_check(
        make_gerber_zip(tmp_path, {"board.gtl": _one_aperture_board("C,0.002000")}, name="tiny.zip"), cd)
    assert tiny.status != "pass"
    assert tiny.metric.measured_value >= 1

    normal = run_single_check(
        make_gerber_zip(tmp_path, {"board.gtl": _one_aperture_board("C,0.250000")}, name="ok.zip"), cd)
    assert normal.status == "pass"


def test_mask_to_trace_clearance_catches_small_opening(tmp_path):
    """Regression: a small-but-real mask opening near a trace must be measured.

    The mask candidate area floor was 0.02 mm^2, which excluded a 0.14 mm
    pad/via opening (0.0196 mm^2) -- a legitimate fine-pitch feature -- so its
    ~0.01 mm encroachment on a neighbouring trace passed silently as
    not_applicable. Lowered to 0.005 mm^2; the violation must now be caught.
    """
    # Trace centreline y=1.0, width 0.2 -> copper edge at y=1.1. Mask opening
    # centred at y=1.18 -> ~0.01 mm clearance (below the 0.025 mm absolute min).
    trace = (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.200000*%\nD10*\n"
        "X1000000Y1000000D02*\nX9000000Y1000000D01*\nM02*\n"
    )
    opening = (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD11R,0.140000X0.140000*%\nD11*\n"
        "X5000000Y1180000D03*\nM02*\n"
    )
    z = make_gerber_zip(tmp_path, {"board.gtl": trace, "board.gts": opening})
    result = run_single_check(z, load_check_definition("mask_to_trace_clearance"))
    assert result.status == "fail"
    assert result.metric.measured_value == pytest.approx(0.01, abs=0.005)


def test_solder_mask_expansion_checks_elongated_pads(tmp_path):
    """Regression: an elongated pad's mask expansion must be evaluated.

    The pad "is this a pad" aspect ceiling was 4.0, which dropped ordinary
    elongated pads (SOIC/connector, aspect > 4) so their mask expansion was never
    checked. A 3.0x0.25 mm pad (aspect 12) with a 4.0x1.25 mm opening (0.5 mm of
    over-expansion, far above the ~0.1 mm max) must now be flagged.
    """
    pad = (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD11R,3.000000X0.250000*%\nD11*\n"
        "X5000000Y5000000D03*\nM02*\n"
    )
    mask = (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD12R,4.000000X1.250000*%\nD12*\n"
        "X5000000Y5000000D03*\nM02*\n"
    )
    z = make_gerber_zip(tmp_path, {"board.gtl": pad, "board.gts": mask, "board.gko": _outline_rect(12.0, 12.0)})
    result = run_single_check(z, load_check_definition("solder_mask_expansion"))
    assert result.status != "pass"
    assert result.metric.measured_value == pytest.approx(0.5, abs=0.05)
