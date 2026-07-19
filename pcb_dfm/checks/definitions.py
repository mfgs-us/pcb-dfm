# pcb_dfm/checks/definitions.py

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Dict, List


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
    Resolve the checks directory from installed package data.

    Assumes data lives at pcb_dfm/check_data/checks/*.json.
    """
    base = files("pcb_dfm")
    return Path(str(base.joinpath("check_data").joinpath("checks")))



def load_check_definition(path: "Path | str") -> CheckDefinition:
    """
    Load a check definition by either a JSON file path or a built-in check id.

    Passing a bare string that is not an existing file path is treated as a
    check id and resolved against the installed check definitions (this is what
    the README examples and the CLI rely on).
    """
    # A string that is not an existing file is treated as a check id.
    if isinstance(path, str) and not Path(path).exists():
        for d in load_all_check_definitions():
            if d.id == path:
                return d
        raise KeyError(f"Unknown check id: {path!r}")

    path = Path(path)
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


def _rulesets_dir() -> Path:
    """Directory holding fab capability profiles (installed package data)."""
    base = files("pcb_dfm")
    return Path(str(base.joinpath("check_data").joinpath("rulesets")))


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``over`` onto ``base`` (dicts merge, everything else
    is replaced). Neither input is mutated."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _merge_profiles(base: Dict[str, Any], child: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a child profile onto a base profile (for ``extends``)."""
    out = dict(base)
    out["overrides"] = _deep_merge(base.get("overrides", {}), child.get("overrides", {}))
    out["policy"] = _deep_merge(base.get("policy", {}), child.get("policy", {}))
    out["disabled_checks"] = sorted(
        set(base.get("disabled_checks", [])) | set(child.get("disabled_checks", [])))
    out["disabled_categories"] = sorted(
        set(base.get("disabled_categories", [])) | set(child.get("disabled_categories", [])))
    if child.get("enabled_checks") is not None:
        out["enabled_checks"] = child["enabled_checks"]
    if child.get("metadata"):
        out["metadata"] = child["metadata"]
    return out


def list_ruleset_ids(rulesets_dir: Path | None = None) -> List[str]:
    """All available ruleset profile ids (filenames without .json)."""
    rulesets_dir = rulesets_dir or _rulesets_dir()
    if not rulesets_dir.exists():
        return ["default"]
    ids = sorted(p.stem for p in rulesets_dir.glob("*.json"))
    return ids or ["default"]


def _load_ruleset_profile(
    ruleset_id: str | None,
    rulesets_dir: Path | None = None,
    _seen: set | None = None,
) -> Dict[str, Any]:
    """Resolve a ruleset profile (following ``extends``) to a merged dict.

    ``default`` (or an empty/None id) resolves to an empty no-op profile even
    when no file is present; any other unknown id is an error.
    """
    rulesets_dir = rulesets_dir or _rulesets_dir()
    _seen = _seen or set()

    name = ruleset_id or "default"
    path = rulesets_dir / f"{name}.json"
    if not path.exists():
        if name == "default":
            return {}
        raise KeyError(
            f"Unknown ruleset {name!r}; available: {list_ruleset_ids(rulesets_dir)}")

    if name in _seen:
        raise ValueError(f"Cyclic ruleset 'extends' involving {name!r}")
    _seen.add(name)

    profile = json.loads(path.read_text())
    parent_id = profile.get("extends")
    if parent_id and parent_id != name:
        parent = _load_ruleset_profile(parent_id, rulesets_dir, _seen)
        profile = _merge_profiles(parent, profile)
    return profile


def load_check_definitions_for_ruleset(
    ruleset_id: str,
    checks_dir: Path | None = None,
) -> List[CheckDefinition]:
    """
    Load the check definitions for a fab capability profile (ruleset).

    The profile (``check_data/rulesets/<ruleset_id>.json``) may:
      - select checks via ``enabled_checks`` (whitelist), ``disabled_checks``,
        and ``disabled_categories``;
      - override any check's fields via ``overrides`` (deep-merged onto the base
        check JSON, e.g. inject a top-level ``limits`` block);
      - set global ``policy`` flags injected into every check (e.g.
        ``strict_plating_mode``, ``fab_clips_silkscreen``).

    ``default`` returns every check unchanged (back-compatible).
    """
    all_defs = load_all_check_definitions(checks_dir=checks_dir)
    profile = _load_ruleset_profile(ruleset_id)
    if not profile:
        return all_defs

    overrides: Dict[str, Any] = profile.get("overrides", {})
    policy: Dict[str, Any] = profile.get("policy", {})
    disabled_checks = set(profile.get("disabled_checks", []))
    disabled_categories = set(profile.get("disabled_categories", []))
    enabled = profile.get("enabled_checks")
    enabled_set = set(enabled) if enabled is not None else None

    result: List[CheckDefinition] = []
    for d in all_defs:
        if enabled_set is not None and d.id not in enabled_set:
            continue
        if d.id in disabled_checks or d.category_id in disabled_categories:
            continue
        merged = dict(d.raw)
        if policy:
            merged = _deep_merge(merged, policy)
        if d.id in overrides:
            merged = _deep_merge(merged, overrides[d.id])
        result.append(CheckDefinition.from_dict(merged))
    return result
