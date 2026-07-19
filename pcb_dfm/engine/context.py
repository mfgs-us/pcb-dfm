# pcb_dfm/engine/context.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..checks.definitions import CheckDefinition
from ..geometry import BoardGeometry
from ..ingest import GerberIngestResult
from .geometry_cache import GeometryCache


@dataclass
class CheckContext:
    """
    Execution context passed into each check runner.

    Carries:
      - the check definition (thresholds etc)
      - ingest metadata
      - geometry model
      - identifiers for ruleset and design
      - optional design_data (stackup / controlled-impedance / net info that is
        not recoverable from bare Gerbers); None when not supplied
    """
    check_def: CheckDefinition
    ingest: GerberIngestResult
    geometry: BoardGeometry
    geometry_cache: GeometryCache
    ruleset_id: str
    design_id: str
    gerber_zip: Path
    design_data: Optional[Dict[str, Any]] = None
