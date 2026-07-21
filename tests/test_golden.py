"""
Golden regression: run the engine over each archetype board and compare a
normalized result digest against a committed baseline. Catches unintended
changes in what the engine reports on a whole board (not just one check).

Regenerate baselines after an intended behavior change:

    PCBDFM_UPDATE_BASELINES=1 pytest tests/test_golden.py
"""

import json
import os
from pathlib import Path

import boards  # tests/boards.py
import pytest

pytest.importorskip("gerber", reason="pcb-tools (gerber) not installed")

_BASELINES = Path(__file__).parent / "baselines"


@pytest.mark.parametrize("name", sorted(boards.ARCHETYPES))
def test_golden(tmp_path, name):
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    z = boards.emit_zip(boards.ARCHETYPES[name](), tmp_path, name=f"{name}.zip")
    digest = boards.result_digest(run_dfm_on_gerber_zip(z, ruleset_id="default"))
    baseline = _BASELINES / f"{name}.json"

    if os.environ.get("PCBDFM_UPDATE_BASELINES"):
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"updated baseline for {name}")

    assert baseline.exists(), (
        f"no baseline for {name}; generate with "
        f"PCBDFM_UPDATE_BASELINES=1 pytest tests/test_golden.py"
    )
    assert digest == json.loads(baseline.read_text()), (
        f"{name} result changed vs baseline; if intended, regenerate with "
        f"PCBDFM_UPDATE_BASELINES=1"
    )
