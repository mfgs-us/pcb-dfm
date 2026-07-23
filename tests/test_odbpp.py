"""ODB++ design-data adapter (#7).

ODB++ is what fabs actually receive, and it carries design intent alongside the
artwork: the layer stack, the netlist, and every component with its pin
locations. That is precisely the input the net-aware and footprint-aware checks
need, from a single source.

These tests run against the committed synthetic job under testdata/odbpp_sample,
built to the documented format. That is weaker evidence than the IPC-D-356
adapter has -- that one was validated against a real vendor export -- so the
adapter documents its supported subset and this suite pins the behaviour that
subset promises, rather than implying broader coverage.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from pcb_dfm.ingest.adapters.odbpp import from_odbpp, looks_like_odbpp  # noqa: E402

_JOB = Path(__file__).resolve().parent.parent / "testdata" / "odbpp_sample"

pytestmark = pytest.mark.skipif(not _JOB.is_dir(), reason="ODB++ fixture missing")


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def test_detects_a_job_by_structure_not_extension():
    """Jobs arrive as bare directories or as archives with no telling suffix, so
    detection keys on the matrix/matrix file every job must have."""
    assert looks_like_odbpp(_JOB)
    assert not looks_like_odbpp(_JOB / "matrix")          # a directory, but not a job
    assert not looks_like_odbpp(_JOB / "matrix" / "matrix")   # the file itself


def test_detects_a_zipped_job(tmp_path):
    z = tmp_path / "job.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in _JOB.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(_JOB.parent).as_posix())
    assert looks_like_odbpp(z)


# --------------------------------------------------------------------------
# Stackup
# --------------------------------------------------------------------------
def test_stackup_keeps_electrical_layers_in_order():
    d = from_odbpp(_JOB)
    assert d.stackup is not None
    got = [(lyr.name, lyr.kind) for lyr in d.stackup.layers]
    assert got == [
        ("top", "copper"),
        ("core1", "dielectric"),
        ("gnd", "copper"),      # POWER_GROUND is copper
        ("bot", "copper"),
    ]


def test_stackup_excludes_non_electrical_layers():
    """Solder mask and the component layer are in the matrix but are not part of
    the electrical stack, and an impedance calculation must not see them."""
    d = from_odbpp(_JOB)
    names = {lyr.name for lyr in d.stackup.layers}
    assert "sm_top" not in names
    assert "comp_+_top" not in names


# --------------------------------------------------------------------------
# Nets and components
# --------------------------------------------------------------------------
def test_nets_are_named_from_eda_data():
    d = from_odbpp(_JOB)
    assert set(d.nets) == {"GND", "VCC", "SIG_A"}


def test_components_carry_side_and_pin_locations():
    d = from_odbpp(_JOB)
    by_ref = {c.ref: c for c in d.components}
    assert set(by_ref) == {"U1", "R1", "C9"}

    u1 = by_ref["U1"]
    assert u1.side == "top"
    assert u1.placed is True
    assert [(p.name, p.x_mm, p.y_mm) for p in u1.pads] == [("1", 9.0, 20.0), ("2", 11.0, 20.0)]

    # Component layers name the side; a part on comp_+_bot is on the bottom.
    assert by_ref["C9"].side == "bottom"
    assert by_ref["R1"].rotation_deg == 90.0


def test_pins_are_linked_to_their_nets():
    """A toeprint carries a net index, which is what lets copper be labelled by
    net -- the same association the IPC-D-356 path provides."""
    d = from_odbpp(_JOB)
    gnd = {(round(p.x_mm, 1), round(p.y_mm, 1)) for p in d.nets["GND"].points}
    assert (11.0, 20.0) in gnd      # U1 pin 2
    assert (30.5, 40.0) in gnd      # R1 pin 2
    assert {(round(p.x_mm, 1), round(p.y_mm, 1)) for p in d.nets["SIG_A"].points} == {(29.5, 40.0)}


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------
def test_units_come_from_the_step_header(tmp_path):
    """ODB++ states units per step, and defaults to INCHES -- so a job that omits
    the header must not be read as millimetres."""
    job = tmp_path / "job"
    shutil.copytree(_JOB, job)
    hdr = job / "steps" / "pcb" / "stephdr"
    hdr.write_text(hdr.read_text().replace("UNITS=MM", "UNITS=INCH"))

    d = from_odbpp(job)
    u1 = {c.ref: c for c in d.components}["U1"]
    assert u1.pads[0].x_mm == pytest.approx(9.0 * 25.4)


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------
def test_load_design_data_auto_detects_an_odbpp_job():
    from pcb_dfm.ingest.design_data import load_design_data

    d = load_design_data(str(_JOB))
    assert d is not None
    assert set(d.nets) == {"GND", "VCC", "SIG_A"}
    assert len(d.components) == 3


def test_a_non_job_directory_is_rejected(tmp_path):
    (tmp_path / "random").mkdir()
    assert not looks_like_odbpp(tmp_path / "random")
    with pytest.raises(ValueError, match="not an ODB\\+\\+ job"):
        from_odbpp(tmp_path / "random")


# --------------------------------------------------------------------------
# End to end: the checks consume it
# --------------------------------------------------------------------------
def test_odbpp_components_feed_pad_identification(tmp_path):
    """The point of the adapter: ODB++ placement data identifies real component
    pads, which is what lets the pad-centric checks measure the right copper."""
    import boards  # tests/boards.py

    pytest.importorskip("gerbonara", reason="gerbonara not installed")
    from pcb_dfm.engine.run import build_geometry_for
    from pcb_dfm.geometry.pad_map import build_pad_map

    # Pads placed where the fixture says U1's pins are.
    board = boards.Board(
        outline=[(0, 0), (40, 0), (40, 30), (0, 30)],
        pads=[boards.Pad(9, 20, 1.0, 1.0), boards.Pad(11, 20, 1.0, 1.0)],
    )
    z = boards.emit_zip(board, tmp_path, name="b.zip")

    pm = build_pad_map(build_geometry_for(z), from_odbpp(_JOB))
    assert pm is not None, "ODB++ placement data should identify pads"
    assert "U1" in pm.components()
