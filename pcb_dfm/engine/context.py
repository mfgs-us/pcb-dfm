# pcb_dfm/engine/context.py

from __future__ import annotations

from dataclasses import dataclass

from pathlib import Path

from ..checks.definitions import CheckDefinition
from ..ingest import GerberIngestResult
from ..geometry import BoardGeometry
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
    """
    check_def: CheckDefinition
    ingest: GerberIngestResult
    geometry: BoardGeometry
    geometry_cache: GeometryCache
    ruleset_id: str
    design_id: str
    gerber_zip: Path
