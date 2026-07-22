"""Trust corpus: run real boards against the full ruleset and assert semantic
invariants (false-positive / false-negative guards). See corpus/README.md.

Unlike the golden baselines (exact digests of synthetic boards), this validates
*confident, human-vouched* expectations on *real* boards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_MANIFESTS = sorted((_REPO / "corpus" / "manifests").glob("*.json"))


def _load(manifest_path):
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _run(manifest):
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    board = _REPO / manifest["board"]
    assert board.exists(), f"corpus board not found: {board}"
    design = manifest.get("design_data")
    design_arg = str(_REPO / design) if design else None
    result = run_dfm_on_gerber_zip(
        board, ruleset_id=manifest.get("ruleset", "default"), design_data=design_arg
    )
    checks = {c.check_id: c for cat in result.categories for c in cat.checks}
    return result, checks


def _crashed(check) -> bool:
    return any((v.message or "").lower().startswith("check crashed") for v in check.violations)


@pytest.mark.skipif(not _MANIFESTS, reason="no corpus manifests")
@pytest.mark.parametrize("manifest_path", _MANIFESTS, ids=lambda p: p.stem)
def test_corpus_board(manifest_path):
    manifest = _load(manifest_path)
    expect = manifest.get("expect", {})
    result, checks = _run(manifest)

    if "min_checks_run" in expect:
        assert len(checks) >= expect["min_checks_run"], (
            f"{len(checks)} checks ran, expected >= {expect['min_checks_run']}")

    if expect.get("no_crashes"):
        crashed = sorted(cid for cid, c in checks.items() if _crashed(c))
        assert not crashed, f"checks crashed: {crashed}"

    if "overall_status" in expect:
        allowed = expect["overall_status"]
        allowed = [allowed] if isinstance(allowed, str) else allowed
        assert result.summary.status in allowed, (
            f"overall status {result.summary.status!r} not in {allowed}")

    for cid, allowed in (expect.get("status") or {}).items():
        assert cid in checks, f"expected check {cid} did not run"
        allowed = [allowed] if isinstance(allowed, str) else allowed
        assert checks[cid].status in allowed, (
            f"{cid}: status {checks[cid].status!r} not in {allowed}")

    for cid in expect.get("must_not_fail", []):
        assert cid in checks, f"expected check {cid} did not run"
        assert checks[cid].status != "fail", (
            f"{cid} failed on a board where it should not (false positive?): "
            f"{checks[cid].violations[0].message if checks[cid].violations else ''}")

    for cid in expect.get("must_fail", []):
        assert cid in checks, f"expected check {cid} did not run"
        assert checks[cid].status == "fail", (
            f"{cid} did not fail on a board with a known defect (false negative?)")
