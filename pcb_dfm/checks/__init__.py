# pcb_dfm/checks/__init__.py

from .definitions import (
    CheckDefinition,
    load_check_definition,
    load_all_check_definitions,
)

# Import implemented checks so they register themselves
from . import impl_copper_to_edge_distance  # noqa: F401

__all__ = [
    "CheckDefinition",
    "load_check_definition",
    "load_all_check_definitions",
]
