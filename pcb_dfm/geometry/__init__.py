# pcb_dfm/geometry/__init__.py

from . import queries as queries
from .gerber_parser import build_board_geometry
from .layer_model import BoardGeometry, BoardLayer
from .primitives import Bounds, Point2D, Polygon

__all__ = [
    "Point2D",
    "Bounds",
    "Polygon",
    "BoardLayer",
    "BoardGeometry",
    "build_board_geometry",
    "queries",
]
