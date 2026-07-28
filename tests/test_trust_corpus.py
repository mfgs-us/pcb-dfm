"""Design-review trust corpus & benchmark harness (#9).

The gerber-only golden corpus (``test_golden.py``) exercises the Tier-1 artwork
checks on real boards, but every Tier-2 *design-review* check is N/A there (no
netlist). Those checks are where the #85 pass fixed a class of real-board false
positives -- GND in a "Power" net class, a battery IC referenced only by
``BAT_NEG``, thin stitching runs on a pour read as a power neck-down. Nothing in
CI guarded them until now.

This module is that guard, in three layers:

* **Whole-board fixtures** (``trust_boards.TRUST_BOARDS``) reproduce those exact
  patterns. Each declares ``must_pass`` -- the checks a correct engine keeps
  clean -- asserted directly, and a full per-check digest is diffed against a
  committed baseline so any status flip (a returning FP, or a lost finding)
  fails CI. This is the CI-protective layer.

* **Property invariants**: a design-review verdict is independent of where the
  board sits (translation) and is reproducible run-to-run (determinism). Either
  breaking points at a geometry or ordering bug.

* **Local real boards** (opt-in via ``PCBDFM_TRUST_BOARDS``): the #85 manual
  pass, institutionalized. Point it at real KiCad projects and it runs the
  design-review suite over each and reports the findings. Skipped when the env
  var is unset (i.e. in CI), because those boards live outside this repo.

Regenerate the fixture baselines after an intended behavior change::

    PCBDFM_UPDATE_BASELINES=1 pytest tests/test_trust_corpus.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import trust_boards  # tests/trust_boards.py

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.checks.definitions import load_all_check_definitions, load_check_definition
from pcb_dfm.engine.check_runner import get_check_runner
from pcb_dfm.engine.context import CheckContext
from pcb_dfm.engine.geometry_cache import GeometryCache
from pcb_dfm.geometry.layer_model import BoardGeometry

_BASELINES = Path(__file__).parent / "baselines" / "trust"


def _design_review_check_ids():
    _ensure_impls_loaded()
    return sorted(
        cd.id for cd in load_all_check_definitions()
        if cd.category_id == "design_advisory"
    )


_DR_CHECKS = _design_review_check_ids()


def _run(cid, dd):
    """Run one design-review check on design data. Returns the CheckResult, or
    None when the check needs gerber artwork (``ctx.ingest``) that a netlist-only
    fixture cannot provide -- those are Tier-1 concerns, covered elsewhere."""
    ctx = CheckContext(
        check_def=load_check_definition(cid), ingest=None,
        geometry=BoardGeometry(root_dir=Path(".")), geometry_cache=GeometryCache(),
        ruleset_id="default", design_id="trust", gerber_zip=Path("x"), design_data=dd)
    try:
        return get_check_runner(cid)(ctx)
    except AttributeError:
        return None  # touched ctx.ingest -> artwork-dependent, not in scope here


def _digest(dd):
    """Status + metric per design-review check that runs on design data alone.

    Checks that need artwork are omitted; the set of keys is therefore part of
    the baseline, so a check dropping out (newly crashing) or joining is caught
    by the diff just like a status change."""
    out = {}
    for cid in _DR_CHECKS:
        r = _run(cid, dd)
        if r is None:
            continue
        mv = r.metric.measured_value if r.metric else None
        out[cid] = {"status": r.status,
                    "metric": (round(mv, 6) if isinstance(mv, float) else mv)}
    return out


# --------------------------------------------------------------------------
# Layer 1: whole-board fixtures, CI-protected.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("board", trust_boards.TRUST_BOARDS, ids=lambda b: b.name)
def test_trust_board_must_pass(board):
    """Every check a correct engine keeps clean on this board is clean -- the
    self-documenting core of each fixture (each entry names the FP class it
    guards)."""
    dd = board.build()
    for cid, why in board.must_pass.items():
        r = _run(cid, dd)
        assert r is not None, f"{cid} did not run on {board.name}"
        assert r.status == "pass", (
            f"{board.name}: {cid} regressed to {r.status!r} (expected pass) -- {why}\n"
            f"  {r.violations[0].message if r.violations else ''}")


@pytest.mark.parametrize("board", trust_boards.TRUST_BOARDS, ids=lambda b: b.name)
def test_trust_board_digest(board):
    """Full per-check digest vs a committed baseline: any status/metric drift on
    a real-board pattern surfaces here, the way #85's bugs first surfaced."""
    digest = _digest(board.build())
    baseline = _BASELINES / f"{board.name}.json"

    if os.environ.get("PCBDFM_UPDATE_BASELINES"):
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"updated trust baseline for {board.name}")

    assert baseline.exists(), (
        f"no trust baseline for {board.name}; generate with "
        f"PCBDFM_UPDATE_BASELINES=1 pytest tests/test_trust_corpus.py")
    assert digest == json.loads(baseline.read_text()), (
        f"{board.name} design-review result changed vs baseline; if intended, "
        f"regenerate with PCBDFM_UPDATE_BASELINES=1")


# --------------------------------------------------------------------------
# Layer 2: property invariants.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("board", trust_boards.TRUST_BOARDS, ids=lambda b: b.name)
def test_translation_invariance(board):
    """A design-review verdict is independent of absolute board position: shift
    every coordinate by a large offset and every status/metric must be identical.
    Catches any check that leaked an absolute coordinate into its logic."""
    base = _digest(board.build())
    shifted = _digest(board.build(offset=(1234.5, -678.9)))
    assert base == shifted, f"{board.name}: design review changed under translation"


@pytest.mark.parametrize("board", trust_boards.TRUST_BOARDS, ids=lambda b: b.name)
def test_determinism(board):
    """The digest is reproducible: dict iteration / set ordering must not leak
    into the reported numbers (a golden baseline is worthless otherwise)."""
    dd = board.build()
    assert _digest(dd) == _digest(dd)


# --------------------------------------------------------------------------
# Layer 3: local real boards (opt-in, skipped in CI).
# --------------------------------------------------------------------------
def _local_boards():
    raw = os.environ.get("PCBDFM_TRUST_BOARDS", "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(os.pathsep):
        p = Path(part).expanduser()
        if p.exists():
            out.append(p)
    return out


_LOCAL = _local_boards()


@pytest.mark.skipif(not _LOCAL, reason="set PCBDFM_TRUST_BOARDS to real board paths")
@pytest.mark.parametrize("board_path", _LOCAL, ids=[p.name for p in _LOCAL])
def test_local_board_design_review(board_path, capsys):
    """Run the design-review suite over a real KiCad project and report its
    findings -- the #85 manual FP hunt, on demand. Fails only on a crash; the
    findings are printed (use ``-s``) for a human to scan for false positives."""
    from pcb_dfm.ingest.design_data import load_design_data

    dd = load_design_data(str(board_path))
    assert dd is not None and dd.nets, f"{board_path}: no design data resolved"
    warnings = []
    for cid in _DR_CHECKS:
        r = _run(cid, dd)
        if r is not None and r.status == "warning":
            msg = r.violations[0].message if r.violations else ""
            warnings.append(f"  {cid}: {msg}")
    with capsys.disabled():
        print(f"\n{board_path.name}: {len(warnings)} design-review warning(s)")
        for w in warnings:
            print(w)
