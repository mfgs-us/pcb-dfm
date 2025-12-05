# pcb_dfm/engine/check_defs.py

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Union

from pcb_dfm.checks.definitions import (
    CheckDefinition as _CheckDefinition,
    load_check_definition as _load_check_definition,
    load_all_check_definitions as _load_all,
)

CheckDefinition = _CheckDefinition

PathLike = Union[str, Path]


def load_check_definition(path: PathLike) -> CheckDefinition:
    """
    Backwards compatible helper to load a single check definition.

    - If given a Path-like -> load JSON from that file.
    - If given a str -> treat as check id and resolve via load_all_check_definitions().
    """
    if isinstance(path, (str, bytes)):
        for d in _load_all():
            if d.id == path:
                return d
        raise KeyError(f"Unknown check id: {path!r}")
    else:
        return _load_check_definition(Path(path))


def load_all_check_definitions() -> List[CheckDefinition]:
    return list(_load_all())
