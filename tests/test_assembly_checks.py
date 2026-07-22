"""wave_solder_shadowing + polarity_marking_consistency (#6 phases 3-4)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset
from pcb_dfm.checks.impl_drill_to_drill_spacing import DrillHole
from pcb_dfm.engine.check_runner import HEURISTIC_CHECK_IDS, get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry
from pcb_dfm.ingest.design_model import Component, DesignData


def _ctx(check_id, ingest, dd):
    _ensure_impls_loaded()
    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}[check_id]
    return CheckContext(
        check_def=cdef, ingest=ingest, geometry=BoardGeometry(root_dir=Path(".")),
        geometry_cache=GeometryCache(), ruleset_id="default", design_id="t",
        gerber_zip=Path("x"), design_data=dd,
    )


def _measured(r):
    m = r.metric
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


# ---------------------------------------------------------------------------
# wave_solder_shadowing
# ---------------------------------------------------------------------------

class _FakeIngest:
    """Minimal ingest exposing drill files + a monkeypatchable drill list."""
    def __init__(self, files):
        self.files = files


def _run_wave(dd, drills, monkeypatch):
    import pcb_dfm.checks.impl_wave_solder_shadowing as M
    monkeypatch.setattr(M, "_collect_drills", lambda ctx: drills)
    ctx = _ctx("wave_solder_shadowing", SimpleNamespace(files=[]), dd)
    return get_check_runner("wave_solder_shadowing")(ctx)


def _tht(ref, x, y, part_class="connector", height=None):
    return Component(ref=ref, x_mm=x, y_mm=y, side="top",
                     part_class=part_class, height_mm=height, placed=True)


def test_wave_shadow_flagged(monkeypatch):
    # Short THT part A at x=0; tall THT part B leading it at x=5 (+x travel).
    a = _tht("J1", 0.0, 0.0, "connector", height=2.0)
    b = _tht("J2", 5.0, 0.0, "connector", height=10.0)
    dd = DesignData(components=[a, b])
    drills = [DrillHole(0.0, 0.0, 0.9), DrillHole(5.0, 0.0, 0.9)]  # holes under both
    r = _run_wave(dd, drills, monkeypatch)
    assert r.status in ("warning", "fail")
    assert _measured(r) > 0.0
    assert any(v.location.component == "J1" for v in r.violations)


def test_wave_no_shadow_passes(monkeypatch):
    # Equal heights -> neither shadows the other.
    a = _tht("J1", 0.0, 0.0, "connector", height=8.0)
    b = _tht("J2", 5.0, 0.0, "connector", height=8.0)
    dd = DesignData(components=[a, b])
    drills = [DrillHole(0.0, 0.0, 0.9), DrillHole(5.0, 0.0, 0.9)]
    r = _run_wave(dd, drills, monkeypatch)
    assert r.status == "pass"
    assert _measured(r) == 0.0


def test_wave_no_tht_is_not_applicable(monkeypatch):
    dd = DesignData(components=[_tht("J1", 0, 0)])
    r = _run_wave(dd, [], monkeypatch)   # no drills
    assert r.status == "not_applicable"


def test_wave_no_components_is_not_applicable(monkeypatch):
    r = _run_wave(DesignData(), [DrillHole(0, 0, 0.9)], monkeypatch)
    assert r.status == "not_applicable"


# ---------------------------------------------------------------------------
# polarity_marking_consistency
# ---------------------------------------------------------------------------

class _SilkFile:
    def __init__(self, path):
        self.path = path
        self.layer_type = "silk"
        self.side = "top"


def _run_polarity(dd, silk_boxes, monkeypatch, tmp_path):
    import pcb_dfm.checks.impl_polarity_marking_consistency as M
    monkeypatch.setattr(M, "_cached_silk_bboxes", lambda path, mtime: silk_boxes)
    f = tmp_path / "top.gto"
    f.write_text("x")
    ctx = _ctx("polarity_marking_consistency", _FakeIngest([_SilkFile(f)]), dd)
    return get_check_runner("polarity_marking_consistency")(ctx)


def _diode(ref, x, y):
    return Component(ref=ref, x_mm=x, y_mm=y, side="top",
                     part_class="diode", polarized=True, placed=True)


def test_polarity_marker_present_passes(monkeypatch, tmp_path):
    dd = DesignData(components=[_diode("D1", 10.0, 10.0)])
    silk = [(9.5, 10.0, 9.5, 10.0)]   # a silk feature right at the part
    r = _run_polarity(dd, silk, monkeypatch, tmp_path)
    assert r.status == "pass"
    assert _measured(r) is True


def test_polarity_marker_missing_warns(monkeypatch, tmp_path):
    dd = DesignData(components=[_diode("D1", 10.0, 10.0)])
    silk = [(50.0, 51.0, 50.0, 51.0)]  # silk far away
    r = _run_polarity(dd, silk, monkeypatch, tmp_path)
    assert r.status == "warning"
    assert _measured(r) is False
    assert r.violations[0].location.component == "D1"


def test_polarity_no_polarized_parts_is_not_applicable(monkeypatch, tmp_path):
    dd = DesignData(components=[
        Component(ref="R1", x_mm=1, y_mm=1, side="top", part_class="resistor",
                  polarized=False, placed=True)])
    r = _run_polarity(dd, [(0, 1, 0, 1)], monkeypatch, tmp_path)
    assert r.status == "not_applicable"


def test_polarity_dnp_excluded(monkeypatch, tmp_path):
    d = _diode("D1", 10.0, 10.0)
    d.dnp = True
    r = _run_polarity(DesignData(components=[d]), [(50, 51, 50, 51)], monkeypatch, tmp_path)
    assert r.status == "not_applicable"   # only polarized part is DNP -> nothing to check


def test_both_labeled_heuristic():
    assert "wave_solder_shadowing" in HEURISTIC_CHECK_IDS
    assert "polarity_marking_consistency" in HEURISTIC_CHECK_IDS
