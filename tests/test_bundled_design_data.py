"""Auto-discovery of design data bundled inside a Gerber package.

A fab commonly ships the netlist (usually IPC-D-356) in the same archive as the
artwork, because it consumes the netlist for electrical test. Until now the
engine ignored it unless the user knew to pass --design-data, so the single
biggest capability -- net-aware and footprint-aware checking -- was dormant for
the most common real-world input.

The one thing that makes silent auto-adoption safe is that it fails closed: a
netlist is adopted only if it registers onto THIS board's own drill hits, so a
stray netlist for a different board is refused rather than mislabelling this one.
These tests pin that, the explicit-override precedence, and the payoff.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.engine.run import run_dfm_on_gerber_zip  # noqa: E402

_TESTDATA = Path(__file__).resolve().parent.parent / "testdata"
_BOARD = _TESTDATA / "pcbtools_full.zip"
_NETLIST = _TESTDATA / "pcbtools_full.ipc"
_OTHER_BOARD = _TESTDATA / "eagle_gyw.zip"

pytestmark = pytest.mark.skipif(
    not (_BOARD.exists() and _NETLIST.exists()),
    reason="corpus board/netlist missing",
)


def _bundle(tmp_path: Path, board: Path, netlist: Path, name: str = "board.ipc") -> Path:
    z = tmp_path / "pkg.zip"
    shutil.copy(board, z)
    with zipfile.ZipFile(z, "a") as zf:
        zf.write(netlist, name)
    return z


def _checks(res):
    return {c.check_id: c for cat in res.categories for c in cat.checks}


def _auto_note(res):
    return [w for w in res.warnings if "auto-discovered" in w]


# --------------------------------------------------------------------------
# The payoff
# --------------------------------------------------------------------------
def test_a_bundled_netlist_is_used_without_a_flag(tmp_path):
    """The netlist sitting in the package lights up the net-aware checks."""
    res = run_dfm_on_gerber_zip(_bundle(tmp_path, _BOARD, _NETLIST), ruleset_id="default")
    checks = _checks(res)

    # These are advisory on artwork alone; a registered netlist resolves the
    # copper the vias connect to, so they pass.
    assert checks["via_to_copper_clearance"].status == "pass"
    assert checks["via_in_pad_thermal_balance"].status == "pass"
    assert _auto_note(res), "the run must disclose that it adopted bundled data"


def test_auto_discovery_matches_passing_the_file_explicitly(tmp_path):
    """Same bytes, whether the netlist is found in the package or named on the
    command line -- discovery must not be a second-class path."""
    auto = run_dfm_on_gerber_zip(
        _bundle(tmp_path, _BOARD, _NETLIST), ruleset_id="default")
    explicit = run_dfm_on_gerber_zip(
        _BOARD, ruleset_id="default", design_data=str(_NETLIST))

    a = {k: v.status for k, v in _checks(auto).items()}
    e = {k: v.status for k, v in _checks(explicit).items()}
    assert a == e


# --------------------------------------------------------------------------
# Fail-closed: the property that makes this safe
# --------------------------------------------------------------------------
def test_a_netlist_for_a_different_board_is_refused(tmp_path):
    """Bundle this board's netlist with a DIFFERENT board. It cannot register
    onto that board's drills, so it must be rejected, not misapplied."""
    if not _OTHER_BOARD.exists():
        pytest.skip("second board missing")

    res = run_dfm_on_gerber_zip(
        _bundle(tmp_path, _OTHER_BOARD, _NETLIST), ruleset_id="default")
    assert not _auto_note(res), "a non-registering netlist must not be adopted"


def test_a_board_without_bundled_data_is_unaffected(tmp_path):
    res = run_dfm_on_gerber_zip(_BOARD, ruleset_id="default")
    assert not _auto_note(res)


def test_a_bundled_non_design_xml_is_not_adopted(tmp_path):
    """Detection is by content: an unrelated .xml in the package (its extension
    overlaps IPC-2581) must not be mistaken for design data."""
    z = tmp_path / "pkg.zip"
    shutil.copy(_BOARD, z)
    with zipfile.ZipFile(z, "a") as zf:
        zf.writestr("notes.xml", "<?xml version='1.0'?><memo>not design data</memo>")

    res = run_dfm_on_gerber_zip(z, ruleset_id="default")
    assert not _auto_note(res)


# --------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------
def test_explicit_design_data_takes_precedence(tmp_path):
    """--design-data wins; the package is not even scanned. Bundle a
    non-registering netlist and pass the real one -- the explicit one is used and
    no auto-discovery note appears."""
    if not _OTHER_BOARD.exists():
        pytest.skip("second board missing")

    # A package whose bundled netlist would be refused for THIS board...
    z = _bundle(tmp_path, _BOARD, _NETLIST)  # (here it happens to match)
    res = run_dfm_on_gerber_zip(z, ruleset_id="default", design_data=str(_NETLIST))
    assert not _auto_note(res), "explicit --design-data must not trigger discovery"
    # and it is genuinely active
    assert _checks(res)["via_to_copper_clearance"].status == "pass"


# --------------------------------------------------------------------------
# The discovery helper directly
# --------------------------------------------------------------------------
def test_discover_prefers_ipc2581_over_a_netlist(tmp_path):
    """When both are present, the richer format (stackup + nets + geometry)
    wins over an access-point-only netlist."""
    from pcb_dfm.ingest.design_data import discover_design_data

    root = tmp_path / "pkg"
    root.mkdir()
    (root / "board.ipc").write_text(_NETLIST.read_text())
    sample = _TESTDATA / "sample_design.xml"
    if not sample.exists():
        pytest.skip("IPC-2581 sample missing")
    (root / "design.xml").write_text(sample.read_text())

    found = discover_design_data(root)
    assert found is not None and found.name == "design.xml"


def test_discover_returns_none_for_a_package_without_design_data(tmp_path):
    from pcb_dfm.ingest.design_data import discover_design_data

    root = tmp_path / "pkg"
    root.mkdir()
    (root / "top.gtl").write_text("%FSLAX46Y46*%\nM02*\n")
    assert discover_design_data(root) is None
