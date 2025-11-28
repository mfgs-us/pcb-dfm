# pcb_dfm/geometry/gerber_parser.py

from __future__ import annotations

from typing import Dict, Tuple

from ..ingest import GerberIngestResult, GerberFileInfo
from .layer_model import BoardLayer, BoardGeometry


def build_board_geometry(ingest: GerberIngestResult) -> BoardGeometry:
    """
    Build a board geometry model from a GerberIngestResult.

    Currently this only organizes files into logical layers and attaches
    them to BoardLayer objects. It does not yet parse real shapes or
    polygons from the Gerber contents; that is an intentional next step.

    Geometry based checks can still inspect which layers exist and
    what files back them, and this structure provides a clear place
    to add real primitives and spatial indices later.
    """
    geom = BoardGeometry(root_dir=ingest.root_dir)

    # Key by (logical_layer, side, layer_type)
    layer_map: Dict[Tuple[str, str, str], BoardLayer] = {}

    for f in ingest.files:
        key = (f.logical_layer, f.side, f.layer_type)
        if key not in layer_map:
            name = f.logical_layer
            layer = BoardLayer(
                name=name,
                logical_layer=f.logical_layer,
                side=f.side,
                layer_type=f.layer_type,
            )
            layer_map[key] = layer
            geom.add_layer(layer)

        layer = layer_map[key]
        layer.file_ids.append(f.id)
        layer.files.append(f)

    return geom
