"""component_to_component_spacing: component identity from placement data (#14).

Clustering copper pads by proximity cannot recover which pads belong to the same
part. Any radius large enough to hold a 2.54 mm-pitch connector together also
merges genuinely separate components; any radius small enough to separate those
components splits the connector against itself and reports a collision between
one part and itself. The placement file simply knows, so when a board supplies
one we use it for identity and keep using the artwork for geometry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.checks import _ensure_impls_loaded  # noqa: E402
from pcb_dfm.checks.definitions import load_check_definitions_for_ruleset  # noqa: E402
from pcb_dfm.engine.check_runner import get_check_runner  # noqa: E402
from pcb_dfm.engine.context import CheckContext  # noqa: E402
from pcb_dfm.engine.geometry_cache import GeometryCache  # noqa: E402
from pcb_dfm.geometry.layer_model import BoardGeometry, BoardLayer  # noqa: E402
from pcb_dfm.geometry.primitives import Point2D, Polygon  # noqa: E402
from pcb_dfm.ingest.design_model import Component, DesignData, Pad  # noqa: E402

_CHECK = "component_to_component_spacing"


def _sq(cx: float, cy: float, size: float = 1.0) -> Polygon:
    h = size / 2.0
    return Polygon(vertices=[
        Point2D(cx - h, cy - h), Point2D(cx + h, cy - h),
        Point2D(cx + h, cy + h), Point2D(cx - h, cy + h),
        Point2D(cx - h, cy - h),
    ])


def _geometry(pad_centers):
    geom = BoardGeometry(root_dir=Path("."))
    top = BoardLayer(name="Top", logical_layer="TopCopper", side="top", layer_type="copper")
    top.polygons = [_sq(x, y) for x, y in pad_centers]
    geom.add_layer(top)
    return geom


def _run(geom, design_data):
    _ensure_impls_loaded()
    cdef = {c.id: c for c in load_check_definitions_for_ruleset("default")}[_CHECK]
    ctx = CheckContext(
        check_def=cdef,
        ingest=type("_I", (), {"files": []})(),
        geometry=geom,
        geometry_cache=GeometryCache(),
        ruleset_id="default",
        design_id="t",
        gerber_zip=Path("x"),
        design_data=design_data,
    )
    return get_check_runner(_CHECK)(ctx)


def _measured(r):
    m = r.metric
    return m.get("measured_value") if isinstance(m, dict) else getattr(m, "measured_value", None)


# A 4-pin connector on 2.54 mm pitch, plus one separate part 6 mm to its right.
# The connector's own pads are 2.54 mm apart -- further than the 1.5 mm default
# cluster radius -- so proximity clustering treats each of its pins as its own
# "component" and reports the 2.54 mm pin pitch (gap 1.54 mm between 1 mm pads)
# as component-to-component spacing.
_CONNECTOR = [(2.0, 5.0), (4.54, 5.0), (7.08, 5.0), (9.62, 5.0)]
_OTHER = [(15.62, 5.0)]
_ALL = _CONNECTOR + _OTHER


def _design_data() -> DesignData:
    return DesignData(components=[
        Component(ref="J1", side="top", placed=True, pads=[
            Pad(str(i + 1), x, y) for i, (x, y) in enumerate(_CONNECTOR)
        ]),
        Component(ref="R1", side="top", placed=True, pads=[
            Pad("1", _OTHER[0][0], _OTHER[0][1])
        ]),
    ])


def test_placement_data_keeps_a_connectors_pins_as_one_component():
    r = _run(_geometry(_ALL), _design_data())
    # J1's rightmost pad is at x=9.62 and R1 sits at x=15.62; both are 1 mm
    # wide, so the real gap between the two parts is 6.0 - 1.0 = 5.0 mm.
    assert _measured(r) == pytest.approx(5.0, abs=1e-6)
    assert r.status == "pass"


def test_without_placement_data_the_pin_pitch_leaks_through():
    """Documents the heuristic's limit, so the win above is not mistaken for luck."""
    r = _run(_geometry(_ALL), None)
    measured = _measured(r)
    # Falls back to clustering: the 2.54 mm pin pitch (1.54 mm pad gap) is
    # reported as if it were spacing between two different components.
    assert measured == pytest.approx(1.54, abs=1e-6)
    assert measured < 5.0


def test_features_with_no_matching_design_pad_are_not_components():
    """A via or fiducial is not a component pad and must not become one."""
    stray = (12.0, 12.0)  # far from every design pad
    r = _run(_geometry(_ALL + [stray]), _design_data())
    # Unchanged: the stray feature is dropped rather than becoming a component
    # that would report a much smaller spacing against J1/R1.
    assert _measured(r) == pytest.approx(5.0, abs=1e-6)


def test_falls_back_when_placement_data_has_no_pads():
    """Identity-only BOM rows (no pad geometry) must not disable the check."""
    dd = DesignData(components=[
        Component(ref="J1", side="top", placed=True),
        Component(ref="R1", side="top", placed=True),
    ])
    r = _run(_geometry(_ALL), dd)
    assert _measured(r) == pytest.approx(1.54, abs=1e-6)
