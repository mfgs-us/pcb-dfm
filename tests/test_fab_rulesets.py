"""
Tests for the real fab capability profiles (rulesets): JLCPCB 2-layer,
JLCPCB 4-layer, PCBWay, and OSH Park.

Verifies each profile is discoverable, loads to a non-empty list of check
definitions, validates against the ruleset-profile JSON schema, and that the
high_speed_si category is included/excluded per the fab's service.
"""

import json
from pathlib import Path

import jsonschema
import pytest

from pcb_dfm.checks.definitions import (
    CheckDefinition,
    list_ruleset_ids,
    load_check_definitions_for_ruleset,
)

FAB_RULESETS = ["jlcpcb_2layer", "jlcpcb_4layer", "pcbway", "oshpark"]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = _REPO_ROOT / "schemas" / "pcb-dfm.ruleset-profile.schema.json"
_RULESETS_DIR = _REPO_ROOT / "pcb_dfm" / "check_data" / "rulesets"


@pytest.fixture(scope="module")
def schema():
    return json.loads(_SCHEMA_PATH.read_text())


@pytest.mark.parametrize("ruleset_id", FAB_RULESETS)
def test_fab_ruleset_is_listed(ruleset_id):
    assert ruleset_id in list_ruleset_ids()


@pytest.mark.parametrize("ruleset_id", FAB_RULESETS)
def test_fab_ruleset_loads_non_empty(ruleset_id):
    defs = load_check_definitions_for_ruleset(ruleset_id)
    assert defs, f"{ruleset_id} produced no check definitions"
    assert all(isinstance(d, CheckDefinition) for d in defs)


@pytest.mark.parametrize("ruleset_id", FAB_RULESETS)
def test_fab_ruleset_validates_against_schema(ruleset_id, schema):
    profile = json.loads((_RULESETS_DIR / f"{ruleset_id}.json").read_text())
    jsonschema.validate(instance=profile, schema=schema)


@pytest.mark.parametrize(
    "ruleset_id,expect_high_speed",
    [
        ("jlcpcb_2layer", False),
        ("oshpark", False),
        ("jlcpcb_4layer", True),
    ],
)
def test_high_speed_si_category_inclusion(ruleset_id, expect_high_speed):
    defs = load_check_definitions_for_ruleset(ruleset_id)
    has_high_speed = any(d.category_id == "high_speed_si" for d in defs)
    assert has_high_speed is expect_high_speed
