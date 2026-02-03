# pcb_dfm/checks/__init__.py

"""
Check definition interfaces and lazy loading of check implementations.

We keep imports of impl_* modules lazy to avoid circular imports with
pcb_dfm.engine.context.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from .definitions import (
    CheckDefinition,
    load_check_definition,
    load_all_check_definitions,
)


def load_all_checks() -> list[ModuleType]:
    """
    Import every module under pcb_dfm.checks so @register_check side effects run.
    Auto-discovers all check modules without hardcoding imports.
    """
    imported = []
    pkg_name = __name__  # "pcb_dfm.checks"
    
    _SKIP_MODULE_SUFFIXES = {
        "all_checks",   # legacy
        "__init__",
    }
    
    for m in pkgutil.walk_packages(__path__, prefix=pkg_name + "."):
        leaf = m.name.split(".")[-1]
        if leaf.startswith("_") or leaf in _SKIP_MODULE_SUFFIXES:
            continue
        imported.append(importlib.import_module(m.name))
    return imported


def _ensure_impls_loaded() -> None:
    """
    Import all check implementation modules so they can register themselves
    with pcb_dfm.engine.check_runner via @register_check.

    This is called lazily by the engine right before running checks, so that
    engine.context is fully initialized and we do not hit circular imports.
    """
    # Copper geometry
    from . import impl_copper_to_edge_distance  # noqa: F401
    from . import impl_min_trace_width  # noqa: F401
    from . import impl_min_trace_spacing  # noqa: F401
    from . import impl_copper_sliver_width  # noqa: F401
    from . import impl_copper_density_balance  # noqa: F401
    from . import impl_acid_trap_angle  # noqa: F401
    from . import impl_plane_fragmentation  # noqa: F401
    from . import impl_copper_thermal_area  # noqa: F401

    # Drill and vias
    from . import impl_min_drill_size  # noqa: F401
    from . import impl_drill_to_drill_spacing  # noqa: F401
    from . import impl_drill_aspect_ratio  # noqa: F401
    from . import impl_min_annular_ring  # noqa: F401
    from . import impl_via_to_copper_clearance  # noqa: F401
    from . import impl_via_tenting  # noqa: F401
    from . import impl_via_in_pad_thermal_balance  # noqa: F401
    from . import impl_drill_wander_budget  # noqa: F401

    # Solder mask and silkscreen
    from . import impl_mask_to_trace_clearance  # noqa: F401
    from . import impl_solder_mask_expansion  # noqa: F401
    from . import impl_solder_mask_web  # noqa: F401
    from . import impl_silkscreen_on_copper  # noqa: F401
    from . import impl_silkscreen_min_width  # noqa: F401
    from . import impl_silkscreen_over_mask_defined_pads  # noqa: F401

    # Assembly / misc
    from . import impl_component_to_component_spacing  # noqa: F401
    from . import impl_missing_tooling_holes  # noqa: F401
    from . import impl_aperture_definition_errors  # noqa: F401
    from . import impl_unsupported_hole_types  # noqa: F401
    from . import impl_thermal_relief_spoke_width  # noqa: F401
    from . import impl_dielectric_thickness_uniformity  # noqa: F401
    from . import impl_impedance_control  # noqa: F401
    from . import impl_backdrill_stub_length  # noqa: F401


__all__ = [
    "CheckDefinition",
    "load_check_definition",
    "load_all_check_definitions",
    "load_all_checks",
    "_ensure_impls_loaded",
]
