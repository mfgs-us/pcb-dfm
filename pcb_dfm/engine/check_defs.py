# pcb_dfm/engine/check_defs.py

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Union

from pcb_dfm.checks import (
    CheckDefinition as _CheckDefinition,
    load_check_definition as _load_check_definition,
    load_all_check_definitions as _load_all,
)

CheckDefinition = _CheckDefinition

PathLike = Union[str, Path]


def load_check_definition(path: PathLike) -> CheckDefinition:
    """
    Backwards compatible helper to load a single check definition.

    Accepts either a Path to a JSON file or a check id (str) if you
    want to resolve against the default checks directory.
    """
    if isinstance(path, (str, bytes)):
        # Treat it as "id" and let the checks module resolve it from package data
        # You may want a dedicated "load_by_id" in pcb_dfm.checks instead.
        # For now, assume `_load_all` + filter.
        for d in _load_all():
            if d.id == path:
                return d
        raise KeyError(f"Unknown check id: {path!r}")
    else:
        return _load_check_definition(Path(path))


def load_all_check_definitions() -> List[CheckDefinition]:
    return list(_load_all())
