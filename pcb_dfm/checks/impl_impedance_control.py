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

    dd = ctx.design_data or {}
    stackup = dd.get("stackup") or {}
    controlled = dd.get("controlled_impedance") or []

    er = stackup.get("er")
    h_mm = stackup.get("dielectric_thickness_mm")
    t_mm = stackup.get("copper_thickness_mm", 0.035)

    have_inputs = (
        isinstance(er, (int, float))
        and isinstance(h_mm, (int, float))
        and isinstance(controlled, list)
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
    for net in controlled:
        name = str(net.get("name", "?"))
        w_mm = net.get("width_mm")
        target_ohm = net.get("target_ohm")
        tol_pct = float(net.get("tolerance_pct", limit_dev_pct))
        if not isinstance(w_mm, (int, float)) or not isinstance(target_ohm, (int, float)):
            continue
        try:
            z0 = _microstrip_z0(float(er), float(h_mm), float(w_mm), float(t_mm))
        except ValueError:
            continue
        dev_pct = abs(z0 - float(target_ohm)) / float(target_ohm) * 100.0
        worst_dev_pct = max(worst_dev_pct, dev_pct)
        if dev_pct > tol_pct:
            violations.append(Violation(
                message=(
                    f"Net {name}: estimated Z0 {z0:.1f} ohm vs target "
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
