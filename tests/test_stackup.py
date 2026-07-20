"""
Stackup honesty: named inner planes (GND/PWR) are detected as inner copper (a
4-layer board is not silently read as 2-layer), the detected stackup is
surfaced on the result, an unclassified copper-looking file warns, and
heuristic checks are labelled.
"""

import zipfile

import pytest

pytest.importorskip("gerber", reason="pcb-tools (gerber) not installed")

_GBODY = (
    "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.100000*%\nD10*\n"
    "X0Y0D02*\nX8000000Y0D01*\nX8000000Y6000000D01*\nX0Y6000000D01*\nX0Y0D01*\nM02*\n"
)
_OUTLINE = (
    "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.100000*%\nD10*\n"
    "X0Y0D02*\nX18000000Y0D01*\nX18000000Y12000000D01*\nX0Y12000000D01*\nX0Y0D01*\nM02*\n"
)


def _zip(tmp_path, names):
    z = tmp_path / "board.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for n in names:
            zf.writestr(n, _OUTLINE if "edge" in n.lower() else _GBODY)
    return z


def _run(z):
    from pcb_dfm.engine.run import run_dfm_on_gerber_zip
    return run_dfm_on_gerber_zip(z, ruleset_id="default", design_id="b")


def test_named_planes_detected_as_four_copper_layers(tmp_path):
    # The reviewer's real board: KiCad planes exported as -GND / -PWR.
    z = _zip(tmp_path, [
        "b-F_Cu.gbr", "b-GND.gbr", "b-PWR.gbr", "b-B_Cu.gbr",
        "b-F_Mask.gbr", "b-F_Silkscreen.gbr", "b-Edge_Cuts.gbr",
    ])
    r = _run(z)
    assert r.design.stackup_layers == 4          # NOT 2
    joined = " ".join(r.design.layers)
    assert "TopCopper" in joined and "BottomCopper" in joined
    assert "InnerCopper1" in joined and "InnerCopper2" in joined
    assert not r.warnings


def test_protel_inner_extensions_detected(tmp_path):
    # KiCad protel export: inner copper as .g2/.g3
    z = _zip(tmp_path, ["b.gtl", "b.g2", "b.g3", "b.gbl", "b.gko"])
    r = _run(z)
    assert r.design.stackup_layers == 4


def test_unclassified_copper_file_warns(tmp_path):
    z = _zip(tmp_path, ["b-F_Cu.gbr", "b-B_Cu.gbr", "b-mystery_cu.gbr", "b-Edge_Cuts.gbr"])
    r = _run(z)
    assert any("mystery_cu" in w for w in r.warnings)
    # the unclassified file is NOT counted in the stackup
    assert r.design.stackup_layers == 2


def test_heuristic_checks_are_labelled(tmp_path):
    z = _zip(tmp_path, ["b-F_Cu.gbr", "b-B_Cu.gbr", "b-Edge_Cuts.gbr"])
    r = _run(z)
    by_id = {c.check_id: c for cat in r.categories for c in cat.checks}
    assert by_id["silkscreen_on_copper"].confidence == "heuristic"
    assert by_id["min_trace_width"].confidence == "high"
