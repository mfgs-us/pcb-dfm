"""ODB++ design-data adapter.

ODB++ is the format fabs actually receive, and unlike a Gerber package it
carries design *intent* alongside the artwork: the layer stack, the netlist, and
every component with its pin locations. That is exactly the input the net-aware
and footprint-aware checks need, from a single source.

An ODB++ job is a directory tree (often shipped as an archive):

    <job>/matrix/matrix                          step + layer structure
    <job>/steps/<step>/stephdr                   units for the step
    <job>/steps/<step>/eda/data                  net names
    <job>/steps/<step>/layers/<comp layer>/components
                                                 components, pins and their nets

Supported subset
----------------
Deliberately scoped to what the checks consume, mirroring how ``ipc2581.py``
handles its own format:

  * **stackup** -- layer order and copper/dielectric roles from ``matrix``
  * **nets** -- names from ``eda/data``
  * **components** -- reference designator, side, and per-pin locations from the
    component layers, which feeds pad identification and net labelling

Not parsed: feature geometry (``layers/<layer>/features``). Gerbers remain the
geometry-of-record; this adapter supplies design data only, the same boundary
the KiCad adapter keeps.

Verification caveat
-------------------
This is tested against a synthetic fixture built to the documented format, not
against a vendor-produced job -- unlike the IPC-D-356 adapter, which was
validated on a real board. Real exports vary, so treat unfamiliar constructs as
"unsupported" rather than assuming this covers them.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..design_model import (
    Component,
    DesignData,
    Net,
    NetPoint,
    Pad,
    Stackup,
    StackupLayer,
)

_INCH_TO_MM = 25.4

# matrix/matrix is a block format: LAYER { ... } with KEY=VALUE lines.
_BLOCK_RE = re.compile(r"(\w+)\s*\{([^}]*)\}", re.DOTALL)
_KV_RE = re.compile(r"^\s*([A-Z_]+)\s*=\s*(.*?)\s*$", re.MULTILINE)

# Layer TYPEs that carry copper. Everything else (solder mask, silk, drill,
# component, route) is not part of the electrical stack.
_COPPER_TYPES = {"SIGNAL", "POWER_GROUND", "MIXED"}
_DIELECTRIC_TYPES = {"DIELECTRIC", "PREPREG", "CORE"}


def looks_like_odbpp(source: Union[str, Path]) -> bool:
    """True when the path looks like an ODB++ job.

    Identified by structure -- the ``matrix/matrix`` file every job must have --
    rather than by extension, since jobs arrive as directories or as archives
    with no distinguishing suffix.
    """
    path = Path(source)
    if path.is_dir():
        return _find_matrix(path) is not None
    if path.is_file() and zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                return any(
                    n.replace("\\", "/").endswith("matrix/matrix")
                    for n in zf.namelist()
                )
        except Exception:
            return False
    return False


def _find_matrix(root: Path) -> Optional[Path]:
    """Locate ``matrix/matrix``, allowing one wrapping directory."""
    direct = root / "matrix" / "matrix"
    if direct.is_file():
        return direct
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if child.is_dir():
            candidate = child / "matrix" / "matrix"
            if candidate.is_file():
                return candidate
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _blocks(text: str, kind: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in _BLOCK_RE.finditer(text):
        if m.group(1).upper() != kind:
            continue
        out.append({k.upper(): v for k, v in _KV_RE.findall(m.group(2))})
    return out


def _parse_matrix(matrix_file: Path) -> Tuple[Optional[Stackup], List[str]]:
    """Layer stack and the step names declared in ``matrix/matrix``."""
    text = _read(matrix_file)
    steps = [b.get("NAME", "") for b in _blocks(text, "STEP") if b.get("NAME")]

    layers: List[Tuple[int, StackupLayer]] = []
    for b in _blocks(text, "LAYER"):
        name = b.get("NAME")
        if not name:
            continue
        ltype = (b.get("TYPE") or "").upper()
        if ltype in _COPPER_TYPES:
            kind = "copper"
        elif ltype in _DIELECTRIC_TYPES:
            kind = "dielectric"
        else:
            continue  # mask, silk, drill, component: not part of the stack
        try:
            row = int(b.get("ROW", "0"))
        except ValueError:
            row = 0
        layers.append((row, StackupLayer(name=name, kind=kind)))

    layers.sort(key=lambda t: t[0])
    stack = Stackup(layers=[lyr for _row, lyr in layers]) if layers else None
    return stack, steps


def _step_units_scale(step_dir: Path) -> float:
    """mm per unit for a step. ODB++ states units per step in ``stephdr``."""
    text = _read(step_dir / "stephdr").upper()
    m = re.search(r"^\s*UNITS\s*=\s*(\S+)", text, re.MULTILINE)
    if m and m.group(1).startswith("MM"):
        return 1.0
    return _INCH_TO_MM  # ODB++ default is inches


def _parse_net_names(step_dir: Path) -> Dict[int, str]:
    """Net index -> name, from ``eda/data`` records of the form ``$<n> <name>``."""
    text = _read(step_dir / "eda" / "data")
    nets: Dict[int, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("$"):
            continue
        m = re.match(r"\$(\d+)\s+(\S+)", line)
        if m:
            nets[int(m.group(1))] = m.group(2)
    return nets


def _parse_components(
    step_dir: Path, scale: float, net_names: Dict[int, str]
) -> Tuple[List[Component], Dict[str, List[NetPoint]]]:
    """Components and their pin locations from the component layers.

    Component layers are conventionally ``comp_+_top`` / ``comp_+_bot``. Each
    ``components`` file holds ``CMP`` records (one per part) each followed by
    ``TOP`` records (one per pin, ODB++ calls them toeprints) carrying the pin's
    absolute location and net index.
    """
    components: List[Component] = []
    points_by_net: Dict[str, List[NetPoint]] = {}

    layers_dir = step_dir / "layers"
    if not layers_dir.is_dir():
        return components, points_by_net

    for layer_dir in sorted(layers_dir.iterdir()):
        name = layer_dir.name.lower()
        if not name.startswith("comp_"):
            continue
        side = "bottom" if name.endswith(("bot", "bottom")) else "top"
        text = _read(layer_dir / "components")
        if not text:
            continue

        current: Optional[Component] = None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            head = parts[0].upper()

            if head == "CMP" and len(parts) >= 7:
                # CMP <pkg> <x> <y> <rot> <mirror> <name> [<part>]
                try:
                    x, y, rot = float(parts[2]), float(parts[3]), float(parts[4])
                except ValueError:
                    current = None
                    continue
                current = Component(
                    ref=parts[6], placed=True, side=side,
                    x_mm=x * scale, y_mm=y * scale, rotation_deg=rot,
                )
                components.append(current)

            elif head == "TOP" and current is not None and len(parts) >= 8:
                # TOP <pin#> <x> <y> <rot> <mirror> <net#> <subnet#> [<name>]
                try:
                    px, py = float(parts[2]) * scale, float(parts[3]) * scale
                    net_idx = int(parts[6])
                except ValueError:
                    continue
                pin = parts[8] if len(parts) >= 9 else parts[1]
                current.pads.append(Pad(
                    name=pin, x_mm=px, y_mm=py,
                    pad_type="smd", through_hole=False,
                ))
                net_name = net_names.get(net_idx)
                if net_name:
                    points_by_net.setdefault(net_name, []).append(NetPoint(
                        x_mm=px, y_mm=py, kind="smd",
                        ref=current.ref, pin=pin, layer=side,
                    ))

    return components, points_by_net


def from_odbpp(source: Union[str, Path], step: Optional[str] = None) -> DesignData:
    """Parse an ODB++ job into :class:`DesignData`.

    Accepts a job directory or a zip archive of one. ``step`` selects which step
    to read when a job holds several (a panel plus its board, say); the first
    declared step is used by default.
    """
    path = Path(source)
    tmp: Optional[Path] = None
    if path.is_file() and zipfile.is_zipfile(path):
        import tempfile

        from ...io.cam_bundle import extract_zip_to_dir

        tmp = Path(tempfile.mkdtemp(prefix="pcb_dfm_odbpp_"))
        extract_zip_to_dir(path, tmp)   # path-traversal guarded
        path = tmp

    matrix_file = _find_matrix(path)
    if matrix_file is None:
        raise ValueError(f"not an ODB++ job (no matrix/matrix): {source}")
    job_root = matrix_file.parent.parent

    stackup, steps = _parse_matrix(matrix_file)

    step_name = step or (steps[0] if steps else None)
    step_dir = None
    steps_dir = job_root / "steps"
    if steps_dir.is_dir():
        if step_name and (steps_dir / step_name).is_dir():
            step_dir = steps_dir / step_name
        else:
            # Step names in matrix are conventionally upper-case while the
            # directory may not be; fall back to the first directory present.
            for cand in sorted(steps_dir.iterdir()):
                if cand.is_dir() and (step_name is None or cand.name.lower() == str(step_name).lower()):
                    step_dir = cand
                    break

    nets: Dict[str, Net] = {}
    components: List[Component] = []
    if step_dir is not None:
        scale = _step_units_scale(step_dir)
        net_names = _parse_net_names(step_dir)
        components, points_by_net = _parse_components(step_dir, scale, net_names)
        for name in net_names.values():
            nets[name] = Net(name=name, points=points_by_net.get(name, []))

    return DesignData(stackup=stackup, nets=nets, components=components)
