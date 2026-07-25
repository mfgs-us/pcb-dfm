"""Stackup symmetry -- is the layer build balanced about the board mid-plane?

An asymmetric stackup (different prepreg/copper distribution above vs below the
neutral axis) warps on reflow: the two halves shrink by different amounts and
the board bows. Volume fabricators reject or re-quote asymmetric builds for
exactly this reason, so it is a genuine manufacturability screen -- but only
one a real stackup can answer. Bare Gerbers carry no layer thicknesses, so
without a design-data stackup this reports not_applicable.

The measure is construction symmetry: pair each layer at position *k* from the
top with the layer at position *k* from the bottom. In a balanced build those
mirror partners share a kind (copper mirrors copper, dielectric mirrors
dielectric) and a thickness. We report the largest thickness mismatch across
the mirror pairs, in microns; a kind mismatch (a structurally lopsided stack)
is always a failure regardless of thickness.
"""

from __future__ import annotations

from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation


def _nested_max(metric_cfg: dict, key: str, default: float) -> float:
    node = metric_cfg.get(key)
    if isinstance(node, dict) and isinstance(node.get("max"), (int, float)):
        return float(node["max"])
    if isinstance(node, (int, float)):
        return float(node)
    return default


def _na(ctx: CheckContext, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult(kind="distance", units="um", measured_value=None),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


@register_check("stackup_symmetry")
def run_stackup_symmetry(ctx: CheckContext) -> CheckResult:
    metric_cfg = ctx.check_def.metric or {}
    target_um = _nested_max(metric_cfg, "target", 20.0)
    limit_um = _nested_max(metric_cfg, "limits", 50.0)

    dd = ctx.design_data
    stackup = dd.stackup if dd is not None else None
    layers = list(stackup.layers) if stackup is not None else []

    # Need a real ordered PHYSICAL build to assess symmetry. Two guards:
    #  - at least 3 layers (one mirror pair plus a centre), and
    #  - at least 2 copper layers.
    # The second is what rejects the flat sidecar synthesis. A sidecar that
    # carries only scalar er/thickness (for the impedance / dielectric checks)
    # is turned into `[copper, dielectric, dielectric, ...]` -- one copper on top
    # then every dielectric entry -- which is NOT a physical stack. It has
    # exactly one copper layer, whereas any real build has >= 2, so this cleanly
    # separates it from an ordered `stackup.layers` list (or an IPC-2581/KiCad
    # stackup) without rejecting legitimate multi-dielectric-between-copper
    # builds. Without this, that synthesized list pairs its lone copper against a
    # dielectric and reports a spurious structural-asymmetry FAIL.
    copper_count = sum(1 for ly in layers if ly.kind == "copper")
    if len(layers) < 3 or copper_count < 2:
        return _na(
            ctx,
            "Stackup symmetry needs an ordered physical layer stackup (>= 3 "
            "layers, >= 2 copper) from a design-data source (IPC-2581, KiCad, or "
            "a sidecar 'stackup.layers' list). Not evaluable from Gerbers or from "
            "a scalar-only stackup sidecar.",
        )

    n = len(layers)
    max_dev_um = 0.0
    structural_mismatch: Optional[str] = None
    compared_pairs = 0
    worst_desc = ""

    for i in range(n // 2):
        a = layers[i]
        b = layers[n - 1 - i]

        if a.kind != b.kind:
            structural_mismatch = (
                f"layer {i + 1} ({a.kind}) mirrors layer {n - i} ({b.kind})"
            )
            # A kind mismatch dominates; keep scanning only to report the worst
            # thickness deviation among comparable pairs, but this already fails.
            continue

        if a.thickness_mm is None or b.thickness_mm is None:
            continue

        compared_pairs += 1
        dev_um = abs(a.thickness_mm - b.thickness_mm) * 1000.0
        if dev_um > max_dev_um:
            max_dev_um = dev_um
            worst_desc = (
                f"layer {i + 1} ({a.thickness_mm * 1000.0:.0f} um) vs layer "
                f"{n - i} ({b.thickness_mm * 1000.0:.0f} um)"
            )

    if structural_mismatch is None and compared_pairs == 0:
        return _na(
            ctx,
            "Stackup layers carry no thicknesses to compare; provide per-layer "
            "thickness_mm to evaluate symmetry.",
        )

    if structural_mismatch is not None:
        status = "fail"
    elif max_dev_um > limit_um:
        status = "fail"
    elif max_dev_um > target_um:
        status = "warning"
    else:
        status = "pass"

    if structural_mismatch is not None:
        score: Optional[float] = 0.0
    elif max_dev_um <= target_um:
        score = 100.0
    elif max_dev_um >= limit_um:
        score = 0.0
    else:
        span = max(1e-12, limit_um - target_um)
        score = max(0.0, min(100.0, 100.0 * (limit_um - max_dev_um) / span))

    violations: List[Violation] = []
    if structural_mismatch is not None:
        violations.append(Violation(
            severity=ctx.check_def.severity,
            message=(
                f"Stackup is structurally asymmetric: {structural_mismatch}. An "
                f"unbalanced build warps on reflow; mirror the layer construction "
                f"about the board mid-plane."
            ),
            location=None,
        ))
    elif status != "pass":
        violations.append(Violation(
            severity=ctx.check_def.severity,
            message=(
                f"Stackup asymmetry: {worst_desc} differ by {max_dev_um:.0f} um "
                f"across the mid-plane (target <= {target_um:.0f} um, absolute "
                f"<= {limit_um:.0f} um). Balance the build to limit warpage."
            ),
            location=None,
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",  # overridden by finalize()
        score=score,
        metric=MetricResult(
            kind="distance",
            units="um",
            measured_value=float(max_dev_um),
            target=target_um,
            limit_high=limit_um,
        ),
        violations=violations,
    ).finalize()
