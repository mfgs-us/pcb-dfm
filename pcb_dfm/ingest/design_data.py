"""
Design-data loading: normalize any supported input into the internal
``DesignData`` model.

Supported sources (see also ``pcb_dfm.ingest.adapters``):
  * ``None``                      -> None
  * a ``DesignData`` instance     -> returned as-is
  * a ``dict``                    -> JSON sidecar shape (adapters.sidecar)
  * a path to ``*.json``          -> parsed then treated as the sidecar shape
  * a path to IPC-2581 XML        -> adapters.ipc2581
  * a path to ODB++               -> not yet implemented (planned adapter)

Bare Gerbers carry no connectivity or stackup, so this is how the impedance,
dielectric-uniformity, and differential-pair checks obtain the data they need.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .adapters import from_ipc2581, from_sidecar, looks_like_ipc2581
from .design_model import DesignData

DesignDataLike = Union[DesignData, Dict[str, Any], str, Path, None]


def load_design_data(source: DesignDataLike) -> Optional[DesignData]:
    if source is None:
        return None
    if isinstance(source, DesignData):
        return source
    if isinstance(source, dict):
        return from_sidecar(source)

    path = Path(source)
    if not path.exists():
        raise ValueError(f"design-data file not found: {path}")

    if looks_like_ipc2581(path):
        return from_ipc2581(path)

    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("design-data JSON must be an object at the top level")
        return from_sidecar(data)

    raise ValueError(
        f"unrecognized design-data source: {path} "
        f"(expected a .json sidecar or an IPC-2581 .xml file)"
    )
