"""BOM (CSV) ingestion + merge onto placement (#6, phase 1)."""

from __future__ import annotations

from pcb_dfm.ingest.adapters.bom import bom_components, expand_designators
from pcb_dfm.ingest.design_data import load_design_data, merge_bom
from pcb_dfm.ingest.design_model import Component, DesignData

# --- designator expansion ----------------------------------------------------

def test_expand_separators_and_ranges():
    assert expand_designators("R1, R2 R3;R5") == ["R1", "R2", "R3", "R5"]
    assert expand_designators("R1-R4") == ["R1", "R2", "R3", "R4"]
    assert expand_designators("C1-3") == ["C1", "C2", "C3"]


def test_expand_dedup_and_reverse_range_left_literal():
    assert expand_designators("R1, R1") == ["R1"]
    assert expand_designators("R5-R3") == ["R5-R3"]  # not a valid ascending range


# --- CSV parsing (messy, real-world) -----------------------------------------

_BOM = """This is a preamble line exported by some tool
Project: widget

Designator,Comment,Manufacturer Part Number,Qty,DNP,Description
"R1, R2, R3",10k,RC0402FR-0710KL,3,,Thick film resistor
C1,100nF,CL05B104KO5NNNC,1,No,Ceramic capacitor
C7,10uF,T491A106K016AT,1,,Tantalum capacitor 16V
U1,MCU,STM32F103,1,,32-bit microcontroller
R9,DNF,,1,Yes,Do not fit
"""


def test_bom_parses_identity_and_expands():
    comps = {c.ref: c for c in bom_components(_BOM)}
    assert set(comps) == {"R1", "R2", "R3", "C1", "C7", "U1", "R9"}
    assert comps["R1"].part_number == "RC0402FR-0710KL"
    assert comps["R1"].part_class == "resistor"
    assert comps["R1"].polarized is False
    assert all(not comps[r].placed for r in comps)  # identity-only


def test_bom_derives_polarity_and_class():
    comps = {c.ref: c for c in bom_components(_BOM)}
    assert comps["C1"].part_class == "capacitor" and comps["C1"].polarized is False
    assert comps["C7"].part_class == "electrolytic" and comps["C7"].polarized is True
    assert comps["U1"].part_class == "ic" and comps["U1"].polarized is True


def test_bom_dnp_parsing():
    comps = {c.ref: c for c in bom_components(_BOM)}
    assert comps["R9"].dnp is True        # explicit DNP=Yes
    assert comps["C1"].dnp is False       # (no DNP column value)


def test_no_header_returns_empty():
    assert bom_components("just,some,random\n1,2,3\n") == []


# --- merge onto placement ----------------------------------------------------

def test_merge_enriches_placement_by_refdes():
    base = DesignData(components=[
        Component(ref="R1", x_mm=1.0, y_mm=2.0, side="top", footprint="R_0402"),
    ])
    bom = DesignData(components=bom_components(_BOM))
    merged = merge_bom(base, bom)

    r1 = {c.ref: c for c in merged.components}["R1"]
    assert r1.x_mm == 1.0 and r1.part_number == "RC0402FR-0710KL"  # geometry + identity
    # BOM lines with no placement come in un-placed; a placed-but-unlisted note appears.
    assert any(c.ref == "C7" and not c.placed for c in merged.components)
    assert any("not placed" in w for w in merged.warnings)


def test_load_design_data_with_bom_kwarg(tmp_path):
    base = DesignData(components=[Component(ref="C7", x_mm=5.0, y_mm=5.0, side="top")])
    bom_file = tmp_path / "bom.csv"
    bom_file.write_text(_BOM, encoding="utf-8")
    merged = load_design_data(base, bom=bom_file)
    c7 = {c.ref: c for c in merged.components}["C7"]
    assert c7.polarized is True and c7.x_mm == 5.0
