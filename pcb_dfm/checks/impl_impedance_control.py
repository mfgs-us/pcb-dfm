from __future__ import annotations

import math

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation, ViolationLocation


def _nested_max(metric_cfg: dict, key: str, default: float) -> float:
    """Read metric["target"]["max"] / metric["limits"]["max"] defensively."""
    node = metric_cfg.get(key)
    if isinstance(node, dict) and isinstance(node.get("max"), (int, float)):
        return float(node["max"])
    if isinstance(node, (int, float)):
        return float(node)
    return default


def _microstrip_z0(er: float, h_mm: float, w_mm: float, t_mm: float) -> float:
    """
    IPC-2141 single-ended surface-microstrip impedance estimate (ohms):

        Z0 = 87 / sqrt(Er + 1.41) * ln( 5.98*h / (0.8*w + t) )

    A first-order approximation valid roughly for 0.1 < w/h < 2.0. Returns a
    positive impedance or raises ValueError on degenerate geometry.
    """
    denom = 0.8 * w_mm + t_mm
    if er <= 0 or h_mm <= 0 or denom <= 0:
        raise ValueError("degenerate stackup/trace geometry")
    arg = 5.98 * h_mm / denom
    if arg <= 0:
        raise ValueError("non-physical microstrip ratio")
    return (87.0 / math.sqrt(er + 1.41)) * math.log(arg)


def _stripline_z0(er: float, b_mm: float, w_mm: float, t_mm: float) -> float:
    """
    IPC-2141 single-ended symmetric-stripline impedance estimate (ohms), for an
    inner trace centred between two reference planes ``b`` apart:

        Z0 = 60 / sqrt(Er) * ln( 4*b / (0.67*pi*(0.8*w + t)) )
    """
    denom = 0.67 * math.pi * (0.8 * w_mm + t_mm)
    if er <= 0 or b_mm <= 0 or denom <= 0:
        raise ValueError("degenerate stackup/trace geometry")
    arg = 4.0 * b_mm / denom
    if arg <= 0:
        raise ValueError("non-physical stripline ratio")
    return (60.0 / math.sqrt(er)) * math.log(arg)


def _differential_z(z0: float, geometry: str, s_mm: float, h_mm: float) -> float:
    """Edge-coupled differential impedance from the single-ended Z0 and the
    edge-to-edge gap ``s`` (IPC-2141 coupling approximations):

        microstrip: Zdiff = 2*Z0 * (1 - 0.48 * exp(-0.96 * s/h))
        stripline:  Zdiff = 2*Z0 * (1 - 0.347 * exp(-2.9  * s/h))
    """
    ratio = s_mm / h_mm if h_mm > 0 else 0.0
    if geometry == "stripline":
        k = 0.347 * math.exp(-2.9 * ratio)
    else:
        k = 0.48 * math.exp(-0.96 * ratio)
    return 2.0 * z0 * (1.0 - k)


def _z0_for(spec, er: float, h_mm: float, b_mm: float, t_mm: float) -> float:
    """Single-ended Z0 for a spec's geometry, choosing the microstrip reference
    height ``h`` or the stripline plane-to-plane ``b`` (spec.height_mm overrides
    either)."""
    w_mm = float(spec.width_mm)
    if (spec.geometry or "microstrip").lower() == "stripline":
        return _stripline_z0(er, float(spec.height_mm or b_mm), w_mm, t_mm)
    return _microstrip_z0(er, float(spec.height_mm or h_mm), w_mm, t_mm)


@register_check("impedance_control")
def run_impedance_control(ctx: CheckContext) -> CheckResult:
    """
    Controlled-impedance check.

    A real impedance check needs stackup (Er, dielectric height, copper
    thickness) and the controlled nets (width + target impedance), none of which
    is recoverable from bare Gerbers. When a design-data sidecar supplies them
    (see pcb_dfm.ingest.design_data), we estimate microstrip Z0 per controlled
    net and flag any whose deviation exceeds tolerance. Otherwise we report
    not_applicable with an explanation.
    """
    metric_cfg = ctx.check_def.metric or {}
    target_dev_pct = _nested_max(metric_cfg, "target", 8.0)
    limit_dev_pct = _nested_max(metric_cfg, "limits", 10.0)

    dd = ctx.design_data
    stackup = dd.stackup if dd is not None else None
    controlled = dd.controlled_impedance if dd is not None else []

    er = stackup.er if stackup is not None else None
    h_mm = stackup.dielectric_thickness_mm if stackup is not None else None
    t_mm = (stackup.copper_thickness_mm if stackup is not None else None)
    if t_mm is None:
        t_mm = 0.035  # default 1oz finished copper
    # Plane-to-plane separation for stripline: the total dielectric between the
    # bracketing planes (approximation; a spec's height_mm overrides it).
    b_mm = (sum(stackup.dielectric_thicknesses_mm()) if stackup is not None else None)
    if not b_mm:
        b_mm = h_mm

    have_inputs = (
        isinstance(er, (int, float))
        and isinstance(h_mm, (int, float))
        and len(controlled) > 0
    )

    if not have_inputs:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",
            score=None,
            metric=MetricResult(kind="ratio", units="%", measured_value=None),
            violations=[Violation(
                message=(
                    "Impedance control cannot be validated from Gerber artwork "
                    "alone. Provide a design-data sidecar with 'stackup' (er, "
                    "dielectric_thickness_mm, copper_thickness_mm) and "
                    "'controlled_impedance' (width_mm, target_ohm) to enable it."
                ),
                severity="info",
            )],
        )

    violations = []
    worst_dev_pct = 0.0
    for spec in controlled:
        name = spec.name
        target_ohm = spec.target_ohm
        tol_pct = spec.tolerance_pct
        if not isinstance(spec.width_mm, (int, float)):
            continue
        geometry = (spec.geometry or "microstrip").lower()
        try:
            z0 = _z0_for(spec, float(er), float(h_mm), float(b_mm), float(t_mm))
        except (ValueError, TypeError):
            continue
        # Differential pair (an edge-to-edge gap is given): compare the modelled
        # differential impedance to the differential target.
        if isinstance(spec.spacing_mm, (int, float)) and spec.spacing_mm > 0:
            ref_h = float(spec.height_mm or (b_mm if geometry == "stripline" else h_mm))
            z = _differential_z(z0, geometry, float(spec.spacing_mm), ref_h)
            kind = f"differential {geometry}"
        else:
            z = z0
            kind = f"single-ended {geometry}"
        dev_pct = abs(z - float(target_ohm)) / float(target_ohm) * 100.0
        worst_dev_pct = max(worst_dev_pct, dev_pct)
        if dev_pct > tol_pct:
            violations.append(Violation(
                message=(
                    f"Net {name} ({kind}): estimated Z {z:.1f} ohm vs target "
                    f"{float(target_ohm):.1f} ohm ({dev_pct:.1f}% off, tolerance "
                    f"{tol_pct:.0f}%)."
                ),
                severity=ctx.check_def.severity,
                location=ViolationLocation(net=name),
            ))

    if worst_dev_pct > limit_dev_pct:
        status = "fail"
    elif worst_dev_pct > target_dev_pct:
        status = "warning"
    else:
        status = "pass"

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=None,
        metric=MetricResult.ratio_percent(
            measured_pct=worst_dev_pct,
            target_pct=target_dev_pct,
            limit_high_pct=limit_dev_pct,
        ),
        violations=violations,
    ).finalize()
