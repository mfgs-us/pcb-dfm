# pcb_dfm/geometry/__init__.py

from .primitives import Point2D, Bounds, Polygon
from .layer_model import BoardLayer, BoardGeometry
from .gerber_parser import build_board_geometry

__all__ = [
    "Point2D",
    "Bounds",
    "Polygon",
    "BoardLayer",
    "BoardGeometry",
    "build_board_geometry",
]
