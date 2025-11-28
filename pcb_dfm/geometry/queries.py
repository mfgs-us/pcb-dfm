# pcb_dfm/geometry/queries.py

from __future__ import annotations

from typing import List, Optional, Iterable

from .layer_model import BoardGeometry, BoardLayer
from .primitives import Bounds, Polygon


def get_outline_layer(geom: BoardGeometry) -> Optional[BoardLayer]:
    """
    Return the primary outline layer, if any.

    Right now we:
    - pick the first layer whose layer_type == "outline"
    Later you could support multiple mechanical / outline layers and
    choose by name.
    """
    for layer in geom.layers:
        if layer.layer_type == "outline":
            return layer
    return None


def get_copper_layers(geom: BoardGeometry) -> List[BoardLayer]:
    """
    Return all copper layers (top, bottom, inner).
    """
    return geom.get_layers_by_type("copper")


def get_mask_layers(geom: BoardGeometry) -> List[BoardLayer]:
    """
    Return all solder mask layers.
    """
    return geom.get_layers_by_type("mask")


def get_silkscreen_layers(geom: BoardGeometry) -> List[BoardLayer]:
    """
    Return all silkscreen layers.
    """
    return geom.get_layers_by_type("silkscreen")


def get_board_bounds(geom: BoardGeometry) -> Optional[Bounds]:
    """
    Compute global board bounds in mm.

    Priority:
    - If outline layer has polygons, use those.
    - Else fall back to all polygons across all layers.

    Returns None if no polygons exist anywhere.
    """
    outline = get_outline_layer(geom)
    if outline and outline.polygons:
        return outline.bounds()

    # Fallback: any layer that has polygons
    return geom.board_bounds()


def iter_layer_polygons(layers: Iterable[BoardLayer]) -> Iterable[Polygon]:
    """
    Yield all polygons from the given layers.
    """
    for layer in layers:
        for poly in layer.polygons:
            yield poly


def get_layer_bounds(layer: BoardLayer) -> Optional[Bounds]:
    """
    Get bounds for a single BoardLayer, if it has polygons.
    """
    return layer.bounds()
