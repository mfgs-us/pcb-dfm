# pcb_dfm/io/cam_bundle.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import zipfile
from typing import Optional, Dict, List, Tuple

GERBER_SUFFIXES = {
    ".gbr", ".ger",
    ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gko", ".gm1", ".gml",
}
DRILL_SUFFIXES = {".drl", ".xln"}

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower().strip())

def _is_junk_path(p: Path) -> bool:
    parts = {_norm(x) for x in p.parts}
    if "macosx" in parts:
        return True
    if p.name.lower().startswith("._"):
        return True
    return False

def _pick_by_alias(files: List[Path], aliases: List[str]) -> Optional[Path]:
    al = [_norm(a) for a in aliases]
    for p in files:
        n = _norm(p.name)
        if any(a and a in n for a in al):
            return p
    return None

@dataclass(frozen=True)
class CamBundlePaths:
    root: Path
    # primary layers
    top_copper: Optional[Path]
    bottom_copper: Optional[Path]
    outline: Optional[Path]
    top_mask: Optional[Path]
    bottom_mask: Optional[Path]
    top_silk: Optional[Path]
    bottom_silk: Optional[Path]
    top_paste: Optional[Path]
    bottom_paste: Optional[Path]
    drill_pth: Optional[Path]
    drill_npth: Optional[Path]
    job: Optional[Path]

    @property
    def present_layers(self) -> List[str]:
        out: List[str] = []
        for k in (
            "top_copper","bottom_copper","top_mask","bottom_mask","top_silk","bottom_silk",
            "top_paste","bottom_paste","outline","drill_pth","drill_npth","job"
        ):
            if getattr(self, k) is not None:
                out.append(k)
        return out

def discover_cam_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if _is_junk_path(p):
            continue
        suf = p.suffix.lower()
        if suf in GERBER_SUFFIXES or suf in DRILL_SUFFIXES or p.name.lower().endswith(".gbrjob"):
            files.append(p)
    return files

def classify_cam_layers(files: List[Path], root: Path) -> CamBundlePaths:
    # KiCad + common CAM aliases
    top_cu = _pick_by_alias(files, ["f_cu", "fcu", "topcu", "top_cu", "gtl"])
    bot_cu = _pick_by_alias(files, ["b_cu", "bcu", "bottomcu", "bottom_cu", "gbl"])
    outline = _pick_by_alias(files, ["edge_cuts", "edgecuts", "outline", "gko", "gm1", "gml"])
    top_mask = _pick_by_alias(files, ["f_mask", "fmask", "gts"])
    bot_mask = _pick_by_alias(files, ["b_mask", "bmask", "gbs"])
    top_silk = _pick_by_alias(files, ["f_silkscreen", "fsilkscreen", "gto"])
    bot_silk = _pick_by_alias(files, ["b_silkscreen", "bsilkscreen", "gbo"])
    top_paste = _pick_by_alias(files, ["f_paste", "fpaste", "gtp"])
    bot_paste = _pick_by_alias(files, ["b_paste", "bpaste", "gbp"])
    job = _pick_by_alias(files, ["gbrjob"])

    drills = [p for p in files if p.suffix.lower() == ".drl"]
    drill_pth = None
    drill_npth = None
    for p in drills:
        nn = _norm(p.name)
        if "pth" in nn and drill_pth is None:
            drill_pth = p
        if "npth" in nn and drill_npth is None:
            drill_npth = p

    return CamBundlePaths(
        root=root,
        top_copper=top_cu,
        bottom_copper=bot_cu,
        outline=outline,
        top_mask=top_mask,
        bottom_mask=bot_mask,
        top_silk=top_silk,
        bottom_silk=bot_silk,
        top_paste=top_paste,
        bottom_paste=bot_paste,
        drill_pth=drill_pth,
        drill_npth=drill_npth,
        job=job,
    )

def extract_zip_to_dir(zip_path: Path, out_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(out_dir)

def load_cam_bundle_from_zip(zip_path: Path, tmp_root: Path) -> Tuple[CamBundlePaths, List[Path]]:
    extract_zip_to_dir(zip_path, tmp_root)
    files = discover_cam_files(tmp_root)
    bundle = classify_cam_layers(files, tmp_root)
    return bundle, files
