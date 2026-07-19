from __future__ import annotations

from math import hypot
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation, ViolationLocation

try:
    import gerber
except Exception:  # pragma: no cover - defensive
    gerber = None


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    metric_cfg = ctx.check_def.metric or {}
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}
    target_max = float(target_cfg.get("max", 0.0))
    limit_max = float(limits_cfg.get("max", 20.0))
    return target_max, limit_max


def _severity(ctx: CheckContext) -> str:
    return (
        ctx.check_def.raw.get("severity_default")
        or ctx.check_def.severity
        or "info"
    )


def _collect_drills(ctx: CheckContext) -> List[Tuple[float, float, float]]:
    """Return drill hits as (x_mm, y_mm, diameter_mm)."""
    if gerber is None:
        return []
    out: List[Tuple[float, float, float]] = []
    for f in ctx.ingest.files:
        if f.layer_type != "drill":
            continue
        try:
            layer = gerber.read(str(f.path))
        except Exception:
            continue
        try:
            layer.to_metric()
        except Exception:
            pass
        for hit in getattr(layer, "hits", []) or []:
            try:
                tool = getattr(hit, "tool", None)
                d = float(getattr(tool, "diameter"))
            except Exception:
                continue
            pos = getattr(hit, "position", None)
            if pos is None:
                continue
            try:
                x, y = float(pos[0]), float(pos[1])
            except Exception:
                continue
            out.append((x, y, d))
    return out


def _na(ctx, target_max, limit_max, msg) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.ratio_percent(None, target_pct=target_max, limit_high_pct=limit_max),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


@register_check("tab_routing_mousebites")
def run_tab_routing_mousebites(ctx: CheckContext) -> CheckResult:
    """Detect breakaway-tab mousebite drill patterns and grade them.

    Heuristic (artwork-only):
      - Mousebites are short rows of small, equal-diameter, evenly spaced
        perforation holes. We look for runs of >=3 small drills (default
        <=0.7 mm) that are near-collinear with a consistent, tight pitch.
      - If no such pattern exists, the check is not applicable (many boards
        have no mousebites) - we do not fabricate a value.
      - When present, we report the percentage deviation of the detected
        mousebite geometry (hole diameter and pitch) from typical recommended
        values, which the metric grades against its limit.
    """
    target_max, limit_max = _thresholds(ctx)

    if gerber is None:
        return _na(ctx, target_max, limit_max,
                   "Gerber/Excellon parser unavailable; cannot detect mousebites.")

    raw = ctx.check_def.raw or {}
    max_dia = float(raw.get("mousebite_max_diameter_mm", 0.7))
    max_pitch = float(raw.get("mousebite_max_pitch_mm", 1.5))
    min_pitch = float(raw.get("mousebite_min_pitch_mm", 0.2))
    min_run = int(raw.get("mousebite_min_holes", 3))
    collinear_tol = float(raw.get("mousebite_collinearity_mm", 0.15))
    rec_dia = float(raw.get("recommended_mousebite_diameter_mm", 0.5))
    rec_pitch = float(raw.get("recommended_mousebite_pitch_mm", 0.5))

    drills = _collect_drills(ctx)
    small = [(x, y, d) for (x, y, d) in drills if 0.0 < d <= max_dia]
    if len(small) < min_run:
        return _na(ctx, target_max, limit_max,
                   "No mousebite drill patterns detected (need a row of small perforation holes); not applicable.")

    # Group small drills by (rounded) diameter - a mousebite row uses one tool.
    groups: dict = {}
    for (x, y, d) in small:
        groups.setdefault(round(d, 3), []).append((x, y, d))

    worst_dev: Optional[float] = None
    worst_loc: Optional[Tuple[float, float]] = None
    n_patterns = 0

    for dia, pts in groups.items():
        if len(pts) < min_run:
            continue
        # Cluster points that are within max_pitch of each other (union by
        # nearest-neighbour chaining). Small N so an O(n^2) sweep is fine.
        n = len(pts)
        parent = list(range(n))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for i in range(n):
            for j in range(i + 1, n):
                if hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) <= max_pitch:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[ri] = rj

        clusters: dict = {}
        for i in range(n):
            clusters.setdefault(find(i), []).append(pts[i])

        for members in clusters.values():
            if len(members) < min_run:
                continue
            # Order the run along its dominant axis and check collinearity + pitch.
            xs = [m[0] for m in members]
            ys = [m[1] for m in members]
            span_x = max(xs) - min(xs)
            span_y = max(ys) - min(ys)
            if span_x >= span_y:
                ordered = sorted(members, key=lambda m: m[0])
                perp_span = span_y
            else:
                ordered = sorted(members, key=lambda m: m[1])
                perp_span = span_x
            # Reject clusters that are 2-D blobs rather than a 1-D row.
            if perp_span > collinear_tol:
                continue
            pitches = [
                hypot(ordered[k + 1][0] - ordered[k][0], ordered[k + 1][1] - ordered[k][1])
                for k in range(len(ordered) - 1)
            ]
            pitches = [p for p in pitches if p > 0.0]
            if not pitches:
                continue
            mean_pitch = sum(pitches) / len(pitches)
            if not (min_pitch <= mean_pitch <= max_pitch):
                continue

            # Qualifies as a mousebite row.
            n_patterns += 1
            dia_dev = abs(dia - rec_dia) / rec_dia if rec_dia > 0 else 0.0
            pitch_dev = abs(mean_pitch - rec_pitch) / rec_pitch if rec_pitch > 0 else 0.0
            dev_pct = 100.0 * max(dia_dev, pitch_dev)
            if worst_dev is None or dev_pct > worst_dev:
                worst_dev = dev_pct
                worst_loc = (sum(xs) / len(xs), sum(ys) / len(ys))

    if worst_dev is None:
        return _na(ctx, target_max, limit_max,
                   "No mousebite drill patterns detected (small holes present but not arranged as a perforation row); not applicable.")

    # Grade: deviation is a "lower is better" percentage vs the limit.
    if worst_dev <= 0.5 * limit_max:
        status = "pass"
    elif worst_dev <= limit_max:
        status = "warning"
    else:
        status = "fail"

    if worst_dev <= 0.0:
        score = 100.0
    elif worst_dev >= limit_max:
        score = 0.0
    else:
        score = max(0.0, min(100.0, 100.0 * (1.0 - worst_dev / limit_max)))

    violations: List[Violation] = []
    if status != "pass":
        loc = None
        if worst_loc is not None:
            loc = ViolationLocation(
                layer="Drill", x_mm=worst_loc[0], y_mm=worst_loc[1],
                notes="Mousebite perforation row deviating from recommended geometry.",
            )
        violations.append(Violation(
            severity=_severity(ctx),
            message=(
                f"Detected {n_patterns} mousebite row(s); worst geometry deviates "
                f"{worst_dev:.1f}% from recommended drill/pitch (limit {limit_max:.0f}%)."
            ),
            location=loc,
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.ratio_percent(
            measured_pct=float(worst_dev),
            target_pct=target_max,
            limit_high_pct=limit_max,
        ),
        violations=violations,
    ).finalize()
