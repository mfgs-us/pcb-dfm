# pcb_dfm/geometry/layer_model.py

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict

from ..ingest import GerberFileInfo
from .primitives import Polygon, Bounds


@dataclass
class BoardLayer:
    """
    Logical layer in the board.

    Holds the mapping back to the source files and any polygons
    or features we derive from them.
    """
    name: str
    logical_layer: str
    side: str
    layer_type: str

    file_ids: List[str] = field(default_factory=list)
    files: List[GerberFileInfo] = field(default_factory=list)

    # Later this can be split into traces, pads, planes, mask, etc.
    polygons: List[Polygon] = field(default_factory=list)

    def bounds(self) -> Optional[Bounds]:
        if not self.polygons:
            return None
        it = iter(self.polygons)
        first = next(it)
        b = first.bounds()
        for poly in it:
            b.include_bounds(poly.bounds())
        return b


@dataclass
class BoardGeometry:
    """
    Board level geometry model.

    For now this is a thin wrapper around layers and their files.
    Later it will hold spatial indices and higher level queries.
    """
    root_dir: Path
    layers: List[BoardLayer] = field(default_factory=list)

    # Optional fast lookup maps
    _by_logical_layer: Dict[str, BoardLayer] = field(default_factory=dict, init=False)
    _by_type: Dict[str, List[BoardLayer]] = field(default_factory=dict, init=False)

    def add_layer(self, layer: BoardLayer) -> None:
        self.layers.append(layer)
        self._by_logical_layer[layer.logical_layer] = layer
        self._by_type.setdefault(layer.layer_type, []).append(layer)

    def get_layer(self, logical_layer: str) -> Optional[BoardLayer]:
        return self._by_logical_layer.get(logical_layer)

    def get_layers_by_type(self, layer_type: str) -> List[BoardLayer]:
        return self._by_type.get(layer_type, [])

    def board_bounds(self) -> Optional[Bounds]:
        """
        If polygons exist and at least one has bounds, compute global bounds.

        You will likely want to replace this with something driven by
        the Outline layer once geometry parsing is real.
        """
        all_bounds: Optional[Bounds] = None
        for layer in self.layers:
            lb = layer.bounds()
            if lb is None:
                continue
            if all_bounds is None:
                all_bounds = Bounds(lb.min_x, lb.min_y, lb.max_x, lb.max_y)
            else:
                all_bounds.include_bounds(lb)
        return all_bounds
