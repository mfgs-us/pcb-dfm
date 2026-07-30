"""Shared helpers for the *structural* stackup checks.

The numeric stackup checks (symmetry, dielectric uniformity, impedance) ask what
the layer values are. These ask what shape the stack is: is it a physically
possible build, is it ordered the way its own layer names claim, what lamination
sequence does it imply.

Two things live here because getting them wrong is the main false-positive risk
for every check in the group:

``ordered_stack``
    The gate. A "stackup" reaching a check is not necessarily a physical stack:
    the sidecar adapter synthesizes one from scalar ``er``/``thickness`` fields
    for the impedance and dielectric checks, producing ``[copper, dielectric,
    dielectric, ...]`` -- one copper layer followed by every dielectric entry.
    That is not a build and must never be graded as one. Real builds have >= 2
    copper layers, which separates them cleanly. ``stackup_symmetry`` learned this
    the hard way (see its inline note); this centralises the rule so the next
    check does not have to.

``inner_index``
    Layer-name index parsing, used to validate physical order against naming.
    Deliberately narrow: it recognises the conventions we have actually seen and
    returns None for anything else, because a wrong guess here would report a
    correctly-ordered board as transposed.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation

# Minimum layers/copper for a list to be a gradeable physical stack.
_MIN_LAYERS = 3
_MIN_COPPER = 2

_TOP_TOKENS = ("f.cu", "top", "l1", "layer1", "cmp", "component")
_BOTTOM_TOKENS = ("b.cu", "bot", "bottom", "sol", "solder")

# "In1.Cu" / "In10.Cu" (KiCad), "L3" / "Layer3", "SIG2" / "IN2" / "INNER2".
_INNER_RE = re.compile(
    r"^(?:in|inner|sig|signal|l|layer|gnd|pwr|plane)[\s_\-]*(\d+)",
    re.IGNORECASE,
)


def ordered_stack(ctx: CheckContext) -> Tuple[List, Optional[str]]:
    """The ordered physical layer list, or ``(layers, reason_it_is_unusable)``.

    Returns ``([], reason)`` when there is nothing gradeable, so callers can hand
    ``reason`` straight to a not_applicable result. Never raises.
    """
    dd = getattr(ctx, "design_data", None)
    stackup = getattr(dd, "stackup", None) if dd is not None else None
    layers = list(getattr(stackup, "layers", []) or []) if stackup is not None else []

    if not layers:
        return [], (
            "No design-data stackup. The physical layer stack is not recoverable "
            "from Gerber artwork; supply IPC-2581, ODB++, a KiCad board, or a "
            "sidecar 'stackup.layers' list."
        )

    copper = sum(1 for ly in layers if getattr(ly, "kind", None) == "copper")
    if len(layers) < _MIN_LAYERS or copper < _MIN_COPPER:
        return [], (
            f"Not an ordered physical stackup ({len(layers)} layer(s), {copper} "
            f"copper): needs >= {_MIN_LAYERS} layers and >= {_MIN_COPPER} copper. A "
            f"sidecar carrying only scalar er/thickness synthesizes a single copper "
            f"layer and is not a build."
        )
    return layers, None


def inner_index(name: Optional[str]) -> Optional[int]:
    """Inner-layer ordinal parsed from a copper layer name, or None.

    ``In2.Cu`` -> 2, ``L3`` -> 3, ``SIG4`` -> 4. Returns None for unrecognised
    naming rather than guessing -- a wrong index would report a correctly built
    board as transposed, which is worse than staying quiet.
    """
    if not name:
        return None
    m = _INNER_RE.match(str(name).strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def layer_side(name: Optional[str]) -> Optional[str]:
    """"top" / "bottom" when the layer name says which outer side it is, else None."""
    if not name:
        return None
    low = str(name).strip().lower()
    for tok in _TOP_TOKENS:
        if low == tok or low.startswith(tok):
            return "top"
    for tok in _BOTTOM_TOKENS:
        if low == tok or low.startswith(tok):
            return "bottom"
    return None


def na(ctx: CheckContext, message: str) -> CheckResult:
    """not_applicable with a count metric -- the shape every check here uses."""
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult(kind="count", units="count", measured_value=None),
        violations=[Violation(severity="info", message=message, location=None)],
    ).finalize()


def fault_result(ctx: CheckContext, faults: List[str], notes: List[str],
                 clean_message: str) -> CheckResult:
    """Standard outcome for a fault-counting structural check.

    ``faults`` are violations of the check's rules (status fail, one violation
    each). ``notes`` are informational observations that must never change the
    status -- they ride along on a passing result. Score is binary because these
    are yes/no structural facts, not process margins.
    """
    count = len(faults)
    status = "fail" if count else "pass"
    violations = [
        Violation(severity=ctx.check_def.severity, message=msg, location=None)
        for msg in faults
    ]
    violations.extend(
        Violation(severity="info", message=msg, location=None) for msg in notes
    )
    if not violations:
        violations = [Violation(severity="info", message=clean_message, location=None)]

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",  # finalize() promotes from the violations
        score=0.0 if count else 100.0,
        metric=MetricResult(kind="count", units="count",
                            measured_value=float(count), target=0.0),
        violations=violations,
    ).finalize()
