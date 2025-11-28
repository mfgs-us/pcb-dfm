# pcb_dfm/checks/__init__.py

from .definitions import (
    CheckDefinition,
    load_check_definition,
    load_all_check_definitions,
)

# Import implemented checks so they register themselves
from . import impl_copper_to_edge_distance  # noqa: F401
from . import impl_min_drill_size  # noqa: F401
from . import impl_min_trace_spacing  # noqa: F401
from . import impl_min_trace_width  # noqa: F401
from . import impl_via_to_copper_clearance  # noqa: F401
from . import impl_drill_aspect_ratio  # noqa: F401
from . import impl_copper_sliver_width
from . import impl_drill_to_drill_spacing
from . import impl_mask_to_trace_clearance
from . import impl_min_annular_ring
from . import impl_silkscreen_on_copper

__all__ = [
    "CheckDefinition",
    "load_check_definition",
    "load_all_check_definitions",
]
