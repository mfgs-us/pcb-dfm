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

pytest.importorskip("gerbonara", reason="gerbonara not installed")

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


# --------------------------------------------------------------------------
# Real boards (#9)
#
# The archetypes above are synthetic: they prove logic, not numbers. The corpus
# manifests assert that specific checks must not fail, which catches the big
# regressions but says nothing about the values. A full digest per real board is
# the net that catches quiet drift -- a measurement moving from 0.201 to 0.187
# because a shared helper changed, on artwork no synthetic fixture resembles.
#
# These WILL move whenever a check legitimately improves. That is the point: the
# diff is the review. Several genuine bugs this project has fixed were found by
# reading exactly such a diff, so regenerate deliberately, never reflexively.
# --------------------------------------------------------------------------
_CORPUS = Path(__file__).resolve().parent.parent / "corpus" / "manifests"
_TESTDATA = Path(__file__).resolve().parent.parent / "testdata"


def _corpus_entries():
    if not _CORPUS.is_dir():
        return []
    out = []
    for manifest in sorted(_CORPUS.glob("*.json")):
        spec = json.loads(manifest.read_text())
        board = Path(spec.get("board", ""))
        if not board.is_absolute():
            board = manifest.parent.parent.parent / board
        if board.is_file():
            out.append((spec.get("name", manifest.stem), board))
    return out


_CORPUS_BOARDS = _corpus_entries()


@pytest.mark.skipif(not _CORPUS_BOARDS, reason="no corpus boards present")
@pytest.mark.parametrize("name,board", _CORPUS_BOARDS, ids=[n for n, _ in _CORPUS_BOARDS])
def test_golden_corpus(name, board):
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    # Feed the board's own design data when it ships some, so the golden covers
    # the net-aware and footprint-aware paths rather than only the artwork ones.
    sidecar = _TESTDATA / f"{board.stem}.ipc"
    design_data = str(sidecar) if sidecar.is_file() else None

    digest = boards.result_digest(
        run_dfm_on_gerber_zip(board, ruleset_id="default", design_data=design_data)
    )
    baseline = _BASELINES / "corpus" / f"{name}.json"

    if os.environ.get("PCBDFM_UPDATE_BASELINES"):
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"updated corpus baseline for {name}")

    assert baseline.exists(), (
        f"no corpus baseline for {name}; generate with "
        f"PCBDFM_UPDATE_BASELINES=1 pytest tests/test_golden.py"
    )
    assert digest == json.loads(baseline.read_text()), (
        f"{name} result changed vs baseline; if intended, regenerate with "
        f"PCBDFM_UPDATE_BASELINES=1"
    )


@pytest.mark.skipif(not _CORPUS_BOARDS, reason="no corpus boards present")
def test_real_boards_are_deterministic():
    """A golden baseline is worthless if the engine is not reproducible.

    Runs the largest real board twice in one process; ordering, dict iteration
    and any cached state must not change the reported numbers.
    """
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip

    name, board = max(_CORPUS_BOARDS, key=lambda nb: nb[1].stat().st_size)
    first = boards.result_digest(run_dfm_on_gerber_zip(board, ruleset_id="default"))
    second = boards.result_digest(run_dfm_on_gerber_zip(board, ruleset_id="default"))
    assert first == second, f"{name} produced different results on a second run"
