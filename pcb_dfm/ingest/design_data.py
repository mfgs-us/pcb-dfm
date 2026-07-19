"""
Optional design-data sidecar: stackup / controlled-impedance / net information
that cannot be recovered from bare Gerber artwork.

Bare Gerbers carry no connectivity or layer-stack information, so a whole tier
of DFM checks (controlled impedance, dielectric uniformity, differential pairs,
return paths) can only be *estimated* or must report ``not_applicable``. When
the user supplies a small JSON sidecar describing the stackup and the
controlled nets, those checks can compute real results.

This is intentionally a lightweight, tool-agnostic interchange format — a
stepping stone toward full IPC-2581 / ODB++ ingestion, not a replacement for it.

Expected shape (all keys optional; checks degrade to not_applicable if the
piece they need is absent)::

    {
      "stackup": {
        "er": 4.3,                         # dielectric constant
        "dielectric_thickness_mm": 0.20,   # substrate height under the layer
        "copper_thickness_mm": 0.035,      # finished copper (1oz ~= 0.035mm)
        "dielectric_layers_mm": [0.10, 0.20, 0.20, 0.10]  # per-prepreg/core
      },
      "controlled_impedance": [
        {"name": "USB_D+", "width_mm": 0.20, "target_ohm": 90, "tolerance_pct": 10},
        {"name": "CLK",    "width_mm": 0.18, "target_ohm": 50, "tolerance_pct": 10}
      ]
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

DesignDataLike = Union[Dict[str, Any], str, Path, None]


def load_design_data(source: DesignDataLike) -> Optional[Dict[str, Any]]:
    """
    Normalize a design-data input to a dict (or None).

    Accepts an already-parsed dict, a path to a JSON file, or None. Raises
    ValueError if a provided path does not exist or does not parse to an object.
    """
    if source is None:
        return None
    if isinstance(source, dict):
        return source

    path = Path(source)
    if not path.exists():
        raise ValueError(f"design-data file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("design-data JSON must be an object at the top level")
    return data
