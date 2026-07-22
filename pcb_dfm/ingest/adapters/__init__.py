"""Adapters that map concrete design-data inputs onto the internal
``pcb_dfm.ingest.design_model.DesignData`` model."""

from .bom import from_bom
from .ipc2581 import from_ipc2581, looks_like_ipc2581
from .kicad import from_kicad, looks_like_kicad
from .sidecar import from_sidecar

__all__ = [
    "from_sidecar",
    "from_ipc2581",
    "looks_like_ipc2581",
    "from_kicad",
    "looks_like_kicad",
    "from_bom",
]
