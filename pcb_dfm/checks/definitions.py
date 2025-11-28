# pcb_dfm/checks/definitions.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
import json


@dataclass
class CheckDefinition:
    """
    Lightweight wrapper around a check JSON file.

    We keep the raw dict so that future fields can be accessed
    without changing this class.
    """
    id: str
    name: str
    category_id: str
    severity: str
    metric: Dict[str, Any]
    applies_to: Dict[str, Any]
    limits: Dict[str, Any]
    description: str | None
    raw: Dict[str, Any]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckDefinition":
        cid = data.get("id")
        if not cid:
            raise ValueError("Check definition missing 'id' field")

        name = data.get("name", cid)
        category_id = data.get("category_id", "general")
        severity = data.get("severity", "error")
        metric = data.get("metric", {})
        applies_to = data.get("applies_to", {})
        limits = data.get("limits", {})
        description = data.get("description")
        return cls(
            id=cid,
            name=name,
            category_id=category_id,
            severity=severity,
            metric=metric,
            applies_to=applies_to,
            limits=limits,
            description=description,
            raw=data,
        )


def _default_checks_dir() -> Path:
    """
    Resolve the checks/ directory relative to this file.

    Assumes repo layout:
      repo_root/
        pcb_dfm/
        checks/
    """
    pkg_root = Path(__file__).resolve().parents[1]
    return pkg_root.parent / "checks"


def load_check_definition(path: Path) -> CheckDefinition:
    data = json.loads(path.read_text())
    # If JSON does not have id, fall back to filename stem
    if "id" not in data:
        data["id"] = path.stem
    return CheckDefinition.from_dict(data)


def load_all_check_definitions(checks_dir: Path | None = None) -> List[CheckDefinition]:
    """
    Load all checks/*.json except index.json.

    For now we ignore rulesets and just return everything. Later we
    can filter by ruleset using checks/index.json.
    """
    if checks_dir is None:
        checks_dir = _default_checks_dir()

    if not checks_dir.exists():
        raise FileNotFoundError(f"Checks directory not found: {checks_dir}")

    defs: List[CheckDefinition] = []
    for p in sorted(checks_dir.glob("*.json")):
        if p.name.lower() == "index.json":
            continue
        defs.append(load_check_definition(p))
    return defs
