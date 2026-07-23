"""Tests for the built-in Excellon fallback parser (#17).

This parser only runs for files gerbonara 1.5 refuses (in practice: anything
containing a ``G85`` routed slot, which aborts the whole file and would take
its drills down with it). These tests pin the coordinate-format handling,
because that is where an Excellon reader silently produces plausible-but-wrong
numbers -- a 25.4x or 1000x error looks like a real board, not a crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcb_dfm.geometry.excellon_fallback import parse_excellon_mm


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "board.drl"
    p.write_text(body)
    return p


# --------------------------------------------------------------------------
# G85 slots -- the reason this parser exists
# --------------------------------------------------------------------------
def test_g85_inline_slot(tmp_path):
    p = _write(tmp_path, "M48\nMETRIC,TZ\nT1C0.500\n%\nT1\nX3.0Y8.0G85X6.0Y8.0\nT0\nM30\n")
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    assert len(parsed.slots) == 1
    s = parsed.slots[0]
    assert (s.x1_mm, s.y1_mm, s.x2_mm, s.y2_mm) == (3.0, 8.0, 6.0, 8.0)
    assert s.width_mm == pytest.approx(0.5)


def test_g85_split_across_lines(tmp_path):
    # Some CAM tools emit the slot end point on its own G85 line.
    p = _write(tmp_path, "M48\nMETRIC,TZ\nT1C0.800\n%\nT1\nX2.0Y2.0\nG85X5.0Y2.0\nT0\nM30\n")
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    assert len(parsed.slots) == 1
    s = parsed.slots[0]
    assert (s.x1_mm, s.y1_mm, s.x2_mm, s.y2_mm) == (2.0, 2.0, 5.0, 2.0)
    assert s.width_mm == pytest.approx(0.8)


def test_g85_file_still_yields_its_plain_drills(tmp_path):
    # The capacity regression this parser prevents: one G85 statement must not
    # cost us the ordinary holes in the same file.
    p = _write(
        tmp_path,
        "M48\nMETRIC,TZ\nT1C0.300\nT2C1.000\n%\n"
        "T1\nX1.0Y1.0\nX2.0Y1.0\n"
        "T2\nX3.0Y8.0G85X6.0Y8.0\n"
        "T0\nM30\n",
    )
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    assert len(parsed.slots) == 1
    assert [(h.x_mm, h.y_mm, h.diameter_mm) for h in parsed.hits] == [
        (1.0, 1.0, 0.3),
        (2.0, 1.0, 0.3),
    ]


# --------------------------------------------------------------------------
# Coordinate formats
# --------------------------------------------------------------------------
def test_implied_decimal_metric_trailing_zeros(tmp_path):
    # METRIC,TZ defaults to 3.3: leading zeros suppressed -> pad left to 6.
    p = _write(tmp_path, "M48\nMETRIC,TZ\nT1C0.500\n%\nT1\nX3000Y8000\nT0\nM30\n")
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    assert (parsed.hits[0].x_mm, parsed.hits[0].y_mm) == (3.0, 8.0)


def test_implied_decimal_explicit_format_header(tmp_path):
    p = _write(tmp_path, "M48\nMETRIC,TZ,000.00\nT1C0.500\n%\nT1\nX12345\nY678\nT0\nM30\n")
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    # 3.2 format: 12345 -> 123.45; the second line is modal in X.
    assert parsed.hits[0].x_mm == pytest.approx(123.45)
    assert parsed.hits[1].x_mm == pytest.approx(123.45)
    assert parsed.hits[1].y_mm == pytest.approx(6.78)


def test_implied_decimal_inch_leading_zeros(tmp_path):
    # INCH,LZ defaults to 2.4: trailing zeros suppressed -> pad right to 6.
    p = _write(tmp_path, "M48\nINCH,LZ\nT1C0.0200\n%\nT1\nX01Y02\nT0\nM30\n")
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    h = parsed.hits[0]
    assert h.x_mm == pytest.approx(25.4)      # 1.0000 in
    assert h.y_mm == pytest.approx(50.8)      # 2.0000 in
    assert h.diameter_mm == pytest.approx(0.508)


def test_inch_units_convert_and_m71_switches_midfile(tmp_path):
    p = _write(tmp_path, "M48\nINCH,TZ\nT1C0.1000\n%\nT1\nX1.0Y0.0\nM71\nX10.0Y0.0\nT0\nM30\n")
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    assert parsed.hits[0].x_mm == pytest.approx(25.4)  # inch
    assert parsed.hits[1].x_mm == pytest.approx(10.0)  # mm after M71
    # The tool was declared in the header's inch units.
    assert parsed.hits[0].diameter_mm == pytest.approx(2.54)


# --------------------------------------------------------------------------
# Route mode and structure
# --------------------------------------------------------------------------
def test_route_mode_cut_is_a_slot_and_rapid_move_is_not(tmp_path):
    p = _write(
        tmp_path,
        "M48\nMETRIC,TZ\nT1C1.000\n%\nT1\n"
        "G00X1.0Y1.0\nM15\nG01X5.0Y1.0\nM16\n"
        "T0\nM30\n",
    )
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    assert len(parsed.slots) == 1
    s = parsed.slots[0]
    assert (s.x1_mm, s.y1_mm, s.x2_mm, s.y2_mm) == (1.0, 1.0, 5.0, 1.0)
    assert s.width_mm == pytest.approx(1.0)
    # The G00 positioning move must not be recorded as a drilled hole.
    assert parsed.hits == []


def test_modal_coordinates_carry_over(tmp_path):
    p = _write(tmp_path, "M48\nMETRIC,TZ\nT1C0.500\n%\nT1\nX1.0Y2.0\nX4.0\nY7.0\nT0\nM30\n")
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    assert [(h.x_mm, h.y_mm) for h in parsed.hits] == [(1.0, 2.0), (4.0, 2.0), (4.0, 7.0)]


def test_comments_and_tool_reselection(tmp_path):
    p = _write(
        tmp_path,
        "M48\n; a comment\nMETRIC,TZ\nT1C0.300\nT2C0.900\n%\n"
        "T1\nX1.0Y1.0 ; drill here\nT2\nX2.0Y2.0\nT0\nM30\n",
    )
    parsed = parse_excellon_mm(p)
    assert parsed is not None
    assert [h.diameter_mm for h in parsed.hits] == [pytest.approx(0.3), pytest.approx(0.9)]


def test_no_tool_definitions_returns_none(tmp_path):
    # Distinguishable from "parsed fine, board genuinely has no drills", so a
    # caller never reports an unparseable file as a drill-free board.
    p = _write(tmp_path, "M48\nMETRIC,TZ\n%\nX1.0Y1.0\nM30\n")
    assert parse_excellon_mm(p) is None


def test_unreadable_file_returns_none(tmp_path):
    assert parse_excellon_mm(tmp_path / "does_not_exist.drl") is None


# --------------------------------------------------------------------------
# The fallback is wired into the backend, not just standalone
# --------------------------------------------------------------------------
def test_backend_routes_g85_files_through_the_fallback(tmp_path):
    from pcb_dfm.geometry.gerber_backend import excellon_hits_mm, excellon_slots_mm

    p = _write(
        tmp_path,
        "M48\nMETRIC,TZ\nT1C0.300\nT2C0.500\n%\n"
        "T1\nX1.0Y1.0\n"
        "T2\nX3.0Y8.0G85X6.0Y8.0\n"
        "T0\nM30\n",
    )
    slots = excellon_slots_mm(p)
    assert len(slots) == 1
    assert slots[0].width_mm == pytest.approx(0.5)

    hits = excellon_hits_mm(p)
    assert [(h.x_mm, h.y_mm, h.diameter_mm) for h in hits] == [(1.0, 1.0, 0.3)]
