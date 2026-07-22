"""plating_uniformity: throwing-power estimate of plated-thickness spread."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset
from pcb_dfm.checks.impl_plating_uniformity import _plating_non_uniformity_pct
from pcb_dfm.engine.check_runner import HEURISTIC_CHECK_IDS, get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry


def test_single_hole_size_is_uniform():
    assert _plating_non_uniformity_pct([0.3, 0.3, 0.3], 1.6, 0.15) == 0.0


def test_wide_aspect_spread_is_high():
    # A 0.2 mm hole (AR 8) plates much thinner than a 2.0 mm hole (AR 0.8).
    v = _plating_non_uniformity_pct([0.2, 2.0], 1.6, 0.15)
    assert v is not None and v > 20.0


def test_monotonic_in_spread():
    narrow = _plating_non_uniformity_pct([0.5, 0.6], 1.6, 0.15)
    wide = _plating_non_uniformity_pct([0.3, 1.5], 1.6, 0.15)
    assert wide > narrow


def test_empty_or_zero_thickness_is_none():
    assert _plating_non_uniformity_pct([], 1.6, 0.15) is None
    assert _plating_non_uniformity_pct([0.3], 0.0, 0.15) is None


def _ctx(ingest):
    _ensure_impls_loaded()
    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}["plating_uniformity"]
    return CheckContext(
        check_def=cdef, ingest=ingest, geometry=BoardGeometry(root_dir=Path(".")),
        geometry_cache=GeometryCache(), ruleset_id="default", design_id="t",
        gerber_zip=Path("x"), design_data=None,
    )


def test_no_drills_is_not_applicable():
    ctx = _ctx(SimpleNamespace(files=[]))
    assert get_check_runner("plating_uniformity")(ctx).status == "not_applicable"


def test_labeled_heuristic():
    assert "plating_uniformity" in HEURISTIC_CHECK_IDS
