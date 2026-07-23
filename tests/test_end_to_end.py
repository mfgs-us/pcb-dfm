"""
End-to-end tests that exercise the real pipeline against a committed minimal
Gerber fixture (testdata/mini_board.zip). Skipped if gerbonara is unavailable.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

_REPO = Path(__file__).resolve().parent.parent
FIXTURE = _REPO / "testdata" / "mini_board.zip"
SCHEMA = _REPO / "schemas" / "dfm-result.schema.json"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(), reason="mini_board.zip fixture not present"
)


def test_run_dfm_bundle_on_fixture():
    from pcb_dfm.engine.run import run_dfm_bundle

    result = run_dfm_bundle(FIXTURE, ruleset_id="default", design_id="mini")

    assert result["error"] is None, f"pipeline errored: {result['error']}"
    assert set(result.keys()) == {"overall_score", "check_results", "stats", "error"}
    assert result["stats"]["total"] > 0
    assert 0.0 <= result["overall_score"] <= 100.0
    # check_results is issues-only: every entry is a warning/fail
    for issue in result["check_results"]:
        assert issue["status"] in ("warning", "fail")


def test_full_dfm_result_and_schema():
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    # Runs ALL definitions, including the 15 without implementations. This must
    # NOT crash: unimplemented checks become not_applicable, not a KeyError.
    dfm = run_dfm_on_gerber_zip(FIXTURE, ruleset_id="default", design_id="mini")

    assert dfm.categories, "expected at least one category"
    total_checks = sum(len(c.checks) for c in dfm.categories)
    assert total_checks > 0

    # Every finalized check has a severity consistent with its status when it
    # has no violations (the invariant finalize() enforces).
    for cat in dfm.categories:
        for chk in cat.checks:
            if not chk.violations:
                if chk.status in ("pass", "not_applicable"):
                    assert chk.severity == "info"
                elif chk.status == "warning":
                    assert chk.severity == "warning"

    # The serialized result validates against the published schema.
    schema = json.loads(SCHEMA.read_text())
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(json.loads(dfm.to_json()), schema)


def test_single_check_finalizes():
    from pcb_dfm.checks.definitions import load_check_definition
    from pcb_dfm.engine.check_runner import run_single_check

    check_def = load_check_definition("min_trace_width")
    result = run_single_check(FIXTURE, check_def, ruleset_id="default", design_id="mini")

    # run_single_check now finalizes: no pass+error contradiction.
    if result.status in ("pass", "not_applicable") and not result.violations:
        assert result.severity == "info"
    assert result.score is not None
