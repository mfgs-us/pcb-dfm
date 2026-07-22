"""Per-check remediation guidance (#8) and its surfacing in reports."""

from __future__ import annotations

import boards  # tests/boards.py

from pcb_dfm.checks import _ensure_impls_loaded
from pcb_dfm.engine.check_runner import _REGISTRY
from pcb_dfm.remediation import GUIDANCE, remediation_for
from pcb_dfm.report import generate_markdown_report, generate_pr_summary, generate_text_report


def test_every_registered_check_has_guidance():
    _ensure_impls_loaded()
    missing = sorted(set(_REGISTRY) - set(GUIDANCE))
    assert not missing, f"checks missing remediation guidance: {missing}"


def test_guidance_entries_are_nonempty():
    for cid, rem in GUIDANCE.items():
        assert rem.fix.strip(), cid
        assert rem.impact.strip(), cid


def test_remediation_for_unknown_is_none():
    assert remediation_for("does_not_exist") is None


def _result(tmp_path):
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    z = boards.emit_zip(boards.ARCHETYPES["thin_trace_board"](), tmp_path, name="b.zip")
    return run_dfm_on_gerber_zip(z, ruleset_id="default")


def _failing_check(result):
    for cat in result.categories:
        for chk in cat.checks:
            if chk.status in ("fail", "warning") and remediation_for(chk.check_id):
                return chk.check_id
    return None


def test_reports_include_fix_for_failing_checks(tmp_path):
    result = _result(tmp_path)
    cid = _failing_check(result)
    assert cid is not None, "expected at least one failing check on thin_trace_board"
    fix = remediation_for(cid).fix

    text = generate_text_report(result)
    md = generate_markdown_report(result)
    pr = generate_pr_summary(result)

    assert "fix:" in text and fix in text
    assert "Recommended fixes" in md and fix in md
    assert "Fix:" in pr
