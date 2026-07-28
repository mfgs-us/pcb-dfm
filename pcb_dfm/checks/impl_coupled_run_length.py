"""Coupled parallel run length -- two signal nets coupling over a long distance.

Extends the spacing-based ``crosstalk_estimate`` with the *length* dimension: a
long, close, near-parallel run between two different signal nets couples even if
the spacing alone looks acceptable. Declared diff-pair members are excluded --
they are *meant* to run together.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_net
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._trace_geom import (
    near_parallel,
    parallel_overlap_offset,
    seg_dir,
    segments_by_layer,
    si_relevant_nets,
)


@register_check("coupled_run_length")
def run_coupled_run_length(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No routed nets; not applicable.")
    signal = {name: n for name, n in dd.nets.items()
              if n.has_geometry() and classify_net(name, n.net_class) == "signal"}
    if len(signal) < 2:
        return na(ctx, "Fewer than two routed signal nets; not applicable.")
    # Coupling matters most when a genuinely high-speed net is involved; an
    # ordinary bus routing together (SPI to a header &c.) is expected, not a flag.
    hs = si_relevant_nets(dd)
    if not hs:
        return na(ctx, "No high-speed nets; coupled-run review not applicable.")
    params = ctx.check_def.raw.get("params", {}) or {}
    gap_factor = float(params.get("gap_factor", 3.0))
    min_coupled = float(params.get("min_coupled_mm", 20.0))
    par_tol = float(params.get("parallel_tol_deg", 15.0))

    # Don't flag the two members of a declared diff pair against each other.
    pair_of = {}
    for dp in dd.diff_pairs:
        pair_of[dp.positive] = dp.negative
        pair_of[dp.negative] = dp.positive

    # Bucket segments by layer so we only compare co-planar traces.
    by_layer: Dict[object, List[Tuple[str, tuple, float]]] = defaultdict(list)
    for name, net in signal.items():
        for layer, segs in segments_by_layer(net).items():
            for (seg, w) in segs:
                if w:
                    by_layer[layer].append((name, seg, w))

    coupled: Dict[Tuple[str, str], float] = defaultdict(float)
    for _layer, items in by_layer.items():
        for i in range(len(items)):
            n1, s1, w1 = items[i]
            d1 = seg_dir(s1)
            for j in range(i + 1, len(items)):
                n2, s2, w2 = items[j]
                if n1 == n2 or pair_of.get(n1) == n2:
                    continue
                if n1 not in hs and n2 not in hs:
                    continue  # neither net is high-speed -> coupling not a concern
                if not near_parallel(d1, seg_dir(s2), par_tol):
                    continue
                po = parallel_overlap_offset(s1, s2)
                if po is None:
                    continue
                offset, overlap = po
                w = max(w1, w2)
                if overlap <= 0 or offset <= 1.05 * w:
                    continue
                if (offset - w) < gap_factor * w:
                    key = (n1, n2) if n1 < n2 else (n2, n1)
                    coupled[key] += overlap

    bad = sorted(((k, ln) for k, ln in coupled.items() if ln >= min_coupled),
                 key=lambda kv: -kv[1])
    if bad:
        msg = ("Long close-coupled parallel runs between signal nets: "
               + ", ".join(f"{a}/{b} ({ln:.0f} mm)" for (a, b), ln in bad[:6])
               + f" (>= {min_coupled:.0f} mm) -> crosstalk.")
        return advisory(ctx, True, count_metric(len(bad)), msg)
    return advisory(ctx, False, count_metric(0), "No excessive coupled runs between signal nets.")
