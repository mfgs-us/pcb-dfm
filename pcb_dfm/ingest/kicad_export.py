"""Run the DFM pipeline directly from a KiCad project, via ``kicad-cli``.

This is Tier 2 of #13. The shipped KiCad adapter reads a ``.kicad_pcb`` for
design data only, with Gerbers remaining the geometry-of-record. Tier 3 -- a
native renderer that turns the board file straight into ``BoardGeometry`` -- is
deliberately not built here, because of the hazard the issue calls out first:
poured copper only exists in a ``.kicad_pcb`` if the user refilled zones before
saving. Rendering a stale file would silently measure copper that differs from
what actually gets fabricated, and a DFM tool that quietly measures the wrong
geometry is worse than one that declines.

Shelling out to KiCad's own plotter avoids that entirely. ``kicad-cli`` fills
zones as part of plotting, applies the project's real plot settings, and emits
the same artwork the fab would receive, so the existing Gerber pipeline runs on
authoritative geometry. The only cost is requiring KiCad to be installed.

What this does NOT claim
------------------------
A run from an exported project answers "is this design manufacturable", not "is
this fabrication package correct". Export-time faults -- wrong plot settings, a
missing layer, a scaling mistake -- exist only in the package a user actually
sends, and an export performed here by definition cannot contain them. The
distinction is recorded on the result as its geometry source so a report can
never imply otherwise.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional, Union

# Geometry provenance, recorded on the result.
GEOMETRY_SOURCE_GERBER = "gerber"
GEOMETRY_SOURCE_KICAD_CLI = "kicad-cli-export"

_LAYERS = (
    "F.Cu,B.Cu,F.Mask,B.Mask,F.SilkS,B.SilkS,F.Paste,B.Paste,Edge.Cuts"
)


def kicad_cli_path() -> Optional[str]:
    """Path to ``kicad-cli``, or None when KiCad is not installed."""
    found = shutil.which("kicad-cli")
    if found:
        return found
    # macOS installs the app bundle without putting the CLI on PATH.
    mac = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    return str(mac) if mac.is_file() else None


def looks_like_kicad_project(source: Union[str, Path]) -> bool:
    """True when the input is a KiCad board or a project directory holding one."""
    path = Path(source)
    if path.is_dir():
        return any(path.glob("*.kicad_pcb"))
    return path.suffix.lower() in {".kicad_pcb", ".kicad_pro"}


def _resolve_board(source: Union[str, Path]) -> Path:
    path = Path(source)
    if path.is_dir():
        pcbs = sorted(path.glob("*.kicad_pcb"))
        if not pcbs:
            raise ValueError(f"no .kicad_pcb found in project directory: {path}")
        return pcbs[0]
    if path.suffix.lower() == ".kicad_pro":
        sibling = path.with_suffix(".kicad_pcb")
        if sibling.is_file():
            return sibling
        raise ValueError(f"no .kicad_pcb beside project file: {path}")
    return path


def export_gerber_zip(
    source: Union[str, Path],
    out_zip: Optional[Path] = None,
    *,
    timeout_s: int = 180,
) -> Path:
    """Plot a KiCad board to Gerbers + drill files and zip them.

    Returns the path to a zip the normal ingest can consume. Raises
    ``RuntimeError`` when ``kicad-cli`` is unavailable or the export fails --
    callers should treat that as "cannot analyse this input" rather than
    falling back to some lesser geometry, since there is none to fall back to.
    """
    cli = kicad_cli_path()
    if cli is None:
        raise RuntimeError(
            "kicad-cli not found; install KiCad to analyse a project directly, "
            "or export Gerbers and pass the zip instead"
        )

    board = _resolve_board(source)
    if not board.is_file():
        raise RuntimeError(f"KiCad board not found: {board}")

    work = Path(tempfile.mkdtemp(prefix="pcb_dfm_kicad_"))
    art = work / "art"
    art.mkdir()

    def _run(args: List[str]) -> None:
        proc = subprocess.run(
            [cli, *args], capture_output=True, text=True, timeout=timeout_s,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"kicad-cli {' '.join(args[:3])} failed: "
                f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
            )

    # KiCad fills zones as part of plotting, so the artwork carries real poured
    # copper rather than whatever fill state the file was saved in.
    _run(["pcb", "export", "gerbers",
          "--output", str(art), "--layers", _LAYERS, str(board)])
    _run(["pcb", "export", "drill",
          "--output", str(art), "--format", "excellon", str(board)])

    produced = [p for p in sorted(art.rglob("*")) if p.is_file()]
    if not produced:
        raise RuntimeError("kicad-cli produced no artwork")

    out_zip = Path(out_zip) if out_zip else work / f"{board.stem}.zip"
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in produced:
            zf.write(p, p.name)
    return out_zip
