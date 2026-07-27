"""Duplicate reference designator -- two placed parts sharing a refdes."""

from __future__ import annotations

from collections import Counter

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na


@register_check("duplicate_refdes")
def run_duplicate_refdes(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.components:
        return na(ctx, "No component placement; not applicable.")
    # Placed instances only (a BOM-only identity row is not a placement clash).
    # Skip un-annotated placeholders (KiCad "REF**", any ref with '*' or '?'),
    # which legitimately repeat across mounting holes / fiducials.
    placed = [c.ref for c in dd.components
              if c.ref and getattr(c, "placed", True) and c.x_mm is not None
              and "*" not in c.ref and "?" not in c.ref]
    if not placed:
        return na(ctx, "No placed components; not applicable.")
    counts = Counter(placed)
    dups = sorted(ref for ref, n in counts.items() if n > 1)
    flagged = bool(dups)
    msg = (f"Reference designator(s) used by more than one placed component: "
           f"{', '.join(dups[:8])}.") if flagged else "All reference designators are unique."
    return advisory(ctx, flagged, count_metric(len(dups)), msg)
