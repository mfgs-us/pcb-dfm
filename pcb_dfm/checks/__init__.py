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
from . import impl_component_to_component_spacing
from . import impl_copper_density_balance
from . import impl_plane_fragmentation
from . import impl_thermal_relief_spoke_width
from . import impl_silkscreen_min_width
from . import impl_solder_mask_expansion
from . import impl_solder_mask_web
from . import impl_silkscreen_over_mask_defined_pads
from . import impl_via_tenting
from . import impl_acid_trap_angle
from . import impl_copper_thermal_area
from . import impl_via_in_pad_thermal_balance
from . import impl_missing_tooling_holes
from . import impl_aperture_definition_errors



__all__ = [
    "CheckDefinition",
    "load_check_definition",
    "load_all_check_definitions",
]
