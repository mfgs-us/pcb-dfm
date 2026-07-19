from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from ..results import DfmResult
from .cam_bundle import (
    CamBundlePaths,
    classify_cam_layers,
    discover_cam_files,
    load_cam_bundle_from_zip,
)


def load_dfm_result(path: Path) -> DfmResult:
    text = Path(path).read_text(encoding="utf-8")
    return DfmResult.from_json(text)


def save_dfm_result(result: DfmResult, path: Path) -> None:
    Path(path).write_text(result.to_json(), encoding="utf-8")


def load_checks_index(path: Path) -> Dict[str, dict]:
    """Load checks/index.json and return mapping check_id -> metadata dict."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    index = {}
    for entry in data.get("checks", []):
        index[entry["id"]] = entry
    return index


__all__ = [
    "load_dfm_result",
    "save_dfm_result",
    "load_checks_index",
    "CamBundlePaths",
    "discover_cam_files",
    "classify_cam_layers",
    "load_cam_bundle_from_zip",
]
