# pcb_dfm/ingest/gerber_zip.py

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional

# Cap total uncompressed extraction size to guard against zip bombs (512 MiB).
_MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

GerberFormat = Literal["gerber", "excellon", "unknown"]
LogicalLayer = Literal[
    "TopCopper",
    "BottomCopper",
    "InnerCopper1",
    "InnerCopper2",
    "InnerCopper3",
    "InnerCopper4",
    "TopSolderMask",
    "BottomSolderMask",
    "TopSilkscreen",
    "BottomSilkscreen",
    "Outline",
    "Mechanical",
    "DrillPlated",
    "DrillNonPlated",
    "Other",
]
LayerSide = Literal["Top", "Bottom", "Inner", "None"]
LayerType = Literal["copper", "mask", "silkscreen", "drill", "outline", "mechanical", "other"]
IngestIssueSeverity = Literal["error", "warning"]


@dataclass
class GerberFileInfo:
    id: str
    path: Path
    original_name: str
    extension: str
    format: GerberFormat
    logical_layer: LogicalLayer
    side: LayerSide
    layer_type: LayerType
    is_plated: Optional[bool] = None
    notes: Optional[str] = None


@dataclass
class GerberIngestIssue:
    severity: IngestIssueSeverity
    code: str
    message: str
    file_id: Optional[str] = None
    extra: Dict[str, object] = field(default_factory=dict)


@dataclass
class GerberIngestResult:
    root_dir: Path
    files: List[GerberFileInfo] = field(default_factory=list)
    issues: List[GerberIngestIssue] = field(default_factory=list)
    aperture_warnings: list[dict] | None = None

    has_top_copper: bool = False
    has_bottom_copper: bool = False
    has_outline: bool = False
    has_drills: bool = False

    # Human-readable warnings surfaced to the user (e.g. a Gerber that looks
    # like copper but could not be classified, so it was not counted).
    warnings: List[str] = field(default_factory=list)

    # If ingest created a temporary directory internally, caller may decide to clean it up.
    is_temporary_root: bool = False

    def add_issue(self, severity: IngestIssueSeverity, code: str, message: str, file_id: Optional[str] = None, extra: Optional[Dict[str, object]] = None) -> None:
        self.issues.append(
            GerberIngestIssue(
                severity=severity,
                code=code,
                message=message,
                file_id=file_id,
                extra=extra or {},
            )
        )


def ingest_gerber_zip(zip_path: Path, workspace_root: Optional[Path] = None) -> GerberIngestResult:
    """
    Ingest a Gerber.zip archive.

    - Validates the zip exists and is a zip file.
    - Extracts to a workspace directory.
    - Scans recursively for Gerber and drill like files.
    - Classifies each file into logical layers and types.
    - Detects missing critical layers and records them as ingest issues.

    Returns a GerberIngestResult that the engine can pass to geometry and general checks.
    """
    zip_path = zip_path.resolve()

    if not zip_path.exists():
        raise FileNotFoundError(f"Gerber zip not found: {zip_path}")

    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Path is not a valid zip file: {zip_path}")

    # Decide where to extract
    is_temp = False
    if workspace_root is None:
        temp_dir = tempfile.mkdtemp(prefix="pcb_dfm_gerber_")
        root_dir = Path(temp_dir)
        is_temp = True
    else:
        workspace_root = workspace_root.resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
        # Extract into a subdirectory named after the zip file stem
        root_dir = workspace_root / zip_path.stem
        if root_dir.exists():
            # Clean it out to avoid stale files
            shutil.rmtree(root_dir)
        root_dir.mkdir(parents=True, exist_ok=True)

    # Extract all contents safely: guard against path traversal (Zip Slip) and
    # zip bombs. extractall() is unsafe because a member named "../x" or with an
    # absolute path can write outside root_dir.
    root_resolved = root_dir.resolve()
    total_uncompressed = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            # Enforce a total uncompressed size cap to prevent zip bombs.
            total_uncompressed += member.file_size
            if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"Zip archive uncompressed size exceeds "
                    f"{_MAX_TOTAL_UNCOMPRESSED_BYTES} bytes; refusing to extract."
                )

            # Reject members that resolve outside root_dir.
            dest = (root_dir / member.filename).resolve()
            if dest != root_resolved and root_resolved not in dest.parents:
                # Skip unsafe members (absolute paths or ../ traversal).
                continue

            zf.extract(member, root_dir)

    result = GerberIngestResult(root_dir=root_dir, is_temporary_root=is_temp)

    # Scan for candidate files
    file_counter = 0
    for p in root_dir.rglob("*"):
        if not p.is_file():
            continue

        # Skip junk from macOS zip packs
        if "__macosx" in (part.lower() for part in p.parts):
            continue
        if p.name.lower().startswith("._"):
            continue

        name_lower = p.name.lower()
        ext = p.suffix.lower()

        # Basic filter: ignore obvious junk like readme, png, pdf etc
        if ext in {".txt", ".csv", ".md", ".pdf", ".png", ".jpg", ".jpeg"} and not _looks_like_drill(name_lower):
            continue

        gerber_format = _guess_format(ext, name_lower)
        if gerber_format == "unknown":
            # Keep as "Other" so geometry layer can decide if it cares
            logical_layer = "Other"
            side = "None"
            layer_type = "other"
            is_plated = None
        else:
            logical_layer, side, layer_type, is_plated = _classify_layer(name_lower, ext, gerber_format)

        file_counter += 1
        file_id = f"file_{file_counter:03d}"

        info = GerberFileInfo(
            id=file_id,
            path=p,
            original_name=p.name,
            extension=ext,
            format=gerber_format,
            # _classify_layer returns plain strs that are valid members of the
            # GerberFileInfo Literal fields; mypy can't see that narrowing.
            logical_layer=logical_layer,  # type: ignore[arg-type]
            side=side,  # type: ignore[arg-type]
            layer_type=layer_type,  # type: ignore[arg-type]
            is_plated=is_plated,
        )
        result.files.append(info)

    # Assign distinct InnerCopperN indices to inner copper files (they are all
    # classified with a placeholder), ordered by filename for determinism, so
    # they do not collapse into one logical layer.
    inner = sorted(
        (f for f in result.files if f.side == "Inner" and f.layer_type == "copper"),
        key=lambda f: f.original_name.lower(),
    )
    for i, f in enumerate(inner, start=1):
        f.logical_layer = f"InnerCopper{i}"  # type: ignore[assignment]

    # Warn about Gerber files that look like copper but could not be classified
    # (rather than silently dropping them and mis-reporting the layer count).
    for f in result.files:
        if f.layer_type == "other" and _looks_like_unclassified_copper(
            f.original_name.lower(), f.extension, f.format
        ):
            result.warnings.append(
                f"'{f.original_name}' looks like a copper layer but was not "
                f"classified; it is NOT counted in the stackup. Rename it to a "
                f"recognized layer name (e.g. In1_Cu) if it is inner copper."
            )

    # Compute summary flags
    result.has_top_copper = any(f.logical_layer == "TopCopper" for f in result.files)
    result.has_bottom_copper = any(f.logical_layer == "BottomCopper" for f in result.files)
    result.has_outline = any(f.logical_layer == "Outline" for f in result.files)
    result.has_drills = any(f.layer_type == "drill" for f in result.files)

    # Record missing critical layers as errors (not fatal)
    if not result.has_top_copper:
        result.add_issue(
            severity="error",
            code="missing_top_copper",
            message="No top copper Gerber layer detected in archive.",
        )

    if not result.has_bottom_copper:
        # This might be acceptable for single sided boards, but report it anyway
        result.add_issue(
            severity="warning",
            code="missing_bottom_copper",
            message="No bottom copper Gerber layer detected in archive.",
        )

    if not result.has_outline:
        result.add_issue(
            severity="error",
            code="missing_outline",
            message="No board outline Gerber layer detected in archive.",
        )

    if not result.has_drills:
        result.add_issue(
            severity="error",
            code="no_drill_files",
            message="No drill files detected in archive. Plated holes cannot be checked.",
        )

    # ---- Aperture validation (fast, ingest-time)
    from .aperture_validation import validate_apertures
    warnings = validate_apertures(result.files, min_dim_mm=0.01, max_dim_mm=200.0)
    result.aperture_warnings = [w.__dict__ for w in warnings]

    return result


def _guess_format(ext: str, name_lower: str) -> GerberFormat:
    """
    Rough guess of whether this file is Gerber, Excellon, or unknown.

    We keep this heuristic simple on purpose; geometry layer can be stricter later.
    """
    if ext in {".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gtp", ".gbp", ".gko", ".gml", ".gm1", ".gm2", ".gbr", ".ger", ".gp1", ".gp2"}:
        return "gerber"

    # Protel-style inner copper extensions: .g1, .g2, ... .g15
    if re.fullmatch(r"\.g\d+", ext):
        return "gerber"

    if ext in {".drl", ".xln"} or _looks_like_drill(name_lower):
        return "excellon"

    # Some CAD tools export gerbers without typical extensions
    if any(token in name_lower for token in [
        "top", "bottom", "inner", "mask", "silk", "outline", "edge",
        "gnd", "pwr", "power", "vcc", "ground", "plane", "copper", "cu",
    ]):
        return "gerber"

    return "unknown"


def _looks_like_drill(name_lower: str) -> bool:
    return any(token in name_lower for token in ["drill", "drl", "xln", "excellon", "npth", "pth"])


# Names that clearly indicate a NON-copper role (so we don't misread them as
# an unclassified copper layer, and don't treat them as inner planes).
_NON_COPPER_TOKENS = (
    "mask", "silk", "paste", "outline", "edge", "keepout", "courtyard",
    "drawing", "fab", "assembly", "assy", "note", "comment", "profile",
    "drill", "npth", "pth", "adhesive", "glue", "stencil",
)

# Tokens that indicate an inner copper plane by name.
_INNER_PLANE_TOKENS = (
    "gnd", "ground", "pwr", "power", "vcc", "vdd", "plane", "mid",
)


def _is_inner_copper(name_lower: str, ext: str) -> bool:
    """True if this file is an inner copper layer.

    Covers KiCad indexed names (In1_Cu, inner2, l3_cu), protel inner-copper
    extensions (.g2, .g3, .gp1), and named power/ground planes (GND, PWR, ...).
    """
    if any(tok in name_lower for tok in _NON_COPPER_TOKENS):
        return False
    # protel inner-copper extensions: .g1 .. .g15, .gp1, .gp2
    if re.fullmatch(r"\.g\d+", ext) or ext in {".gp1", ".gp2"}:
        return True
    # explicit inner index: in1_cu / inner2 / l3_cu / in3
    if re.search(r"(in\d+|inner\d+|l[2-9]\d?|l1[0-5])(_?cu)?", name_lower):
        return True
    # named plane, when it also reads as copper (or gives no other role)
    if any(tok in name_lower for tok in _INNER_PLANE_TOKENS):
        return True
    return False


def _looks_like_unclassified_copper(name_lower: str, ext: str, fmt: GerberFormat) -> bool:
    """A Gerber file that fell through to 'other' but might actually be copper —
    worth warning about rather than silently dropping."""
    if fmt != "gerber":
        return False
    if any(tok in name_lower for tok in _NON_COPPER_TOKENS):
        return False
    return "cu" in name_lower or "copper" in name_lower or "sig" in name_lower


def _classify_layer(
    name_lower: str,
    ext: str,
    fmt: GerberFormat,
) -> tuple[LogicalLayer, LayerSide, LayerType, Optional[bool]]:
    # Defaults
    logical_layer: LogicalLayer = "Other"
    side: LayerSide = "None"
    layer_type: LayerType = "other"
    is_plated: Optional[bool] = None

    if fmt == "excellon":
        layer_type = "drill"
        side = "None"

        np_tokens = ["npth", "nonplated", "non-plated", "np_"]
        if any(t in name_lower for t in np_tokens):
            logical_layer = "DrillNonPlated"
            is_plated = False
        else:
            logical_layer = "DrillPlated"
            is_plated = True

        return logical_layer, side, layer_type, is_plated

    # ---- fmt == "gerber" ----

    # 4A) Use extensions first for reliable copper/mask/silk classification
    # This prevents misclassification when filename doesn't include extension in name_lower

    # Copper layers - check extensions first
    if ext == ".gtl":
        logical_layer = "TopCopper"
        side = "Top"
        layer_type = "copper"
        return logical_layer, side, layer_type, is_plated

    if ext == ".gbl":
        logical_layer = "BottomCopper"
        side = "Bottom"
        layer_type = "copper"
        return logical_layer, side, layer_type, is_plated

    # Solder mask - check extensions first
    if ext == ".gts":
        logical_layer = "TopSolderMask"
        side = "Top"
        layer_type = "mask"
        return logical_layer, side, layer_type, is_plated

    if ext == ".gbs":
        logical_layer = "BottomSolderMask"
        side = "Bottom"
        layer_type = "mask"
        return logical_layer, side, layer_type, is_plated

    # Silkscreen - check extensions first
    if ext == ".gto":
        logical_layer = "TopSilkscreen"
        side = "Top"
        layer_type = "silkscreen"
        return logical_layer, side, layer_type, is_plated

    if ext == ".gbo":
        logical_layer = "BottomSilkscreen"
        side = "Bottom"
        layer_type = "silkscreen"
        return logical_layer, side, layer_type, is_plated

    # Paste - check extensions first
    if ext == ".gtp":
        logical_layer = "Other"
        side = "Top"
        layer_type = "other"
        return logical_layer, side, layer_type, is_plated

    if ext == ".gbp":
        logical_layer = "Other"
        side = "Bottom"
        layer_type = "other"
        return logical_layer, side, layer_type, is_plated

    # Outline - check extensions first
    if ext in {".gko", ".gm1", ".gml"}:
        logical_layer = "Outline"
        side = "None"
        layer_type = "outline"
        return logical_layer, side, layer_type, is_plated

    # Mechanical - check extensions first
    if ext == ".gm2":
        logical_layer = "Mechanical"
        side = "None"
        layer_type = "mechanical"
        return logical_layer, side, layer_type, is_plated

    # Copper layers - fallback to name heuristics for .gbr and unknown extensions
    if ("f_cu" in name_lower) or ("fcu" in name_lower) or ("gtl" in name_lower) or ("top" in name_lower and ("cu" in name_lower or "copper" in name_lower or "sig" in name_lower)):
        logical_layer = "TopCopper"
        side = "Top"
        layer_type = "copper"
        return logical_layer, side, layer_type, is_plated

    if ("b_cu" in name_lower) or ("bcu" in name_lower) or ("gbl" in name_lower) or (("bot" in name_lower or "bottom" in name_lower) and ("cu" in name_lower or "copper" in name_lower or "sig" in name_lower)):
        logical_layer = "BottomCopper"
        side = "Bottom"
        layer_type = "copper"
        return logical_layer, side, layer_type, is_plated

    # Inner copper. Beyond KiCad's In1_Cu/In2_Cu, real exports name inner
    # planes GND/PWR/VCC/... and use protel .g2/.g3 extensions. Missing these
    # silently drops inner layers, so a 4-layer board reads as 2-layer. We
    # classify all of them as inner copper here; ingest assigns distinct
    # InnerCopperN indices afterwards. The exact index is resolved in a second
    # pass over the whole fileset (see ingest_gerber_zip).
    if _is_inner_copper(name_lower, ext):
        # Placeholder index; ingest_gerber_zip renumbers inner copper 1..N.
        logical_layer = "InnerCopper1"
        side = "Inner"
        layer_type = "copper"
        return logical_layer, side, layer_type, is_plated

    # Solder mask - fallback to name heuristics
    if ("f_mask" in name_lower) or ("fmask" in name_lower) or ("gts" in name_lower) or (("top" in name_lower) and ("mask" in name_lower)):
        logical_layer = "TopSolderMask"
        side = "Top"
        layer_type = "mask"
        return logical_layer, side, layer_type, is_plated

    if ("b_mask" in name_lower) or ("bmask" in name_lower) or ("gbs" in name_lower) or (("bot" in name_lower or "bottom" in name_lower) and ("mask" in name_lower)):
        logical_layer = "BottomSolderMask"
        side = "Bottom"
        layer_type = "mask"
        return logical_layer, side, layer_type, is_plated

    # Silkscreen - fallback to name heuristics
    if ("f_silkscreen" in name_lower) or ("fsilkscreen" in name_lower) or ("gto" in name_lower) or (("top" in name_lower) and ("silk" in name_lower or "ss" in name_lower)):
        logical_layer = "TopSilkscreen"
        side = "Top"
        layer_type = "silkscreen"
        return logical_layer, side, layer_type, is_plated

    if ("b_silkscreen" in name_lower) or ("bsilkscreen" in name_lower) or ("gbo" in name_lower) or (("bot" in name_lower or "bottom" in name_lower) and ("silk" in name_lower or "ss" in name_lower)):
        logical_layer = "BottomSilkscreen"
        side = "Bottom"
        layer_type = "silkscreen"
        return logical_layer, side, layer_type, is_plated

    # Paste - fallback to name heuristics
    if ("f_paste" in name_lower) or ("fpaste" in name_lower) or ("gtp" in name_lower) or (("top" in name_lower) and ("paste" in name_lower)):
        logical_layer = "Other"
        side = "Top"
        layer_type = "other"
        return logical_layer, side, layer_type, is_plated

    if ("b_paste" in name_lower) or ("bpaste" in name_lower) or ("gbp" in name_lower) or (("bot" in name_lower or "bottom" in name_lower) and ("paste" in name_lower)):
        logical_layer = "Other"
        side = "Bottom"
        layer_type = "other"
        return logical_layer, side, layer_type, is_plated

    # 4B) Outline classification - enhanced for .gbr names like Edge_Cuts
    # Check extensions first (already done above), then name heuristics
    if ("edge_cuts" in name_lower) or ("edgecuts" in name_lower) or any(t in name_lower for t in ["outline", "boardoutline", "boardoutline", "board_edge", "board-edge"]):
        logical_layer = "Outline"
        side = "None"
        layer_type = "outline"
        return logical_layer, side, layer_type, is_plated

    # Mechanical - fallback to name heuristics
    if "mech" in name_lower or "mechanical" in name_lower:
        logical_layer = "Mechanical"
        side = "None"
        layer_type = "mechanical"
        return logical_layer, side, layer_type, is_plated

    return logical_layer, side, layer_type, is_plated
