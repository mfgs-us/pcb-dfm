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


def test_routine_hole_mix_below_knee_is_uniform():
    # Vias + component holes on a normal board (AR 2.0-5.3, all below the knee)
    # must NOT read as a plating problem -- the false positive the corpus caught.
    assert _plating_non_uniformity_pct([0.30, 0.50, 0.80], 1.6, 0.15) == 0.0


def test_high_aspect_hole_diverges():
    # A 0.15 mm via (AR ~10.7, above the knee) beside shallow holes diverges.
    v = _plating_non_uniformity_pct([0.15, 0.8], 1.6, 0.15)
    assert v is not None and v > 20.0


def test_monotonic_in_spread_above_knee():
    narrow = _plating_non_uniformity_pct([0.20, 0.22], 1.6, 0.15)   # AR 8.0, 7.3
    wide = _plating_non_uniformity_pct([0.15, 0.22], 1.6, 0.15)     # AR 10.7, 7.3
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
