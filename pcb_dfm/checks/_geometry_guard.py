"""Shared guard against geometry with implausibly large extents.

Several checks build spatial grids whose cell count scales with the board's
stated size. A corrupt or mis-scaled Gerber -- for instance one carrying a
``999999999999`` coordinate -- therefore makes them allocate billions of cells
and hang the run indefinitely. No such input describes a real board (no PCB or
fab panel approaches two metres), so the honest response is to decline, not to
grind. This helper is what those checks call to do that uniformly.
"""

from __future__ import annotations

from typing import Optional

from ..engine.context import CheckContext
from ..geometry.queries import MAX_PLAUSIBLE_EXTENT_MM, geometry_extent_plausible
from ..results import CheckResult, Violation


def implausible_extent_result(ctx: CheckContext) -> Optional[CheckResult]:
    """A ``not_applicable`` result when the geometry is implausibly large.

    Returns None when the extent is sane, so the caller proceeds normally::

        guard = implausible_extent_result(ctx)
        if guard is not None:
            return guard
    """
    if geometry_extent_plausible(ctx.geometry):
        return None

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=None,
        violations=[
            Violation(
                severity="info",
                message=(
                    f"Board extent exceeds {MAX_PLAUSIBLE_EXTENT_MM:.0f} mm; the "
                    f"artwork appears corrupt or mis-scaled (a real board is far "
                    f"smaller), so this geometry check is not evaluated."
                ),
                location=None,
            )
        ],
    ).finalize()
