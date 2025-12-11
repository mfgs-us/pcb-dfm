"""
pcb_dfm package init.

For now we keep the public API minimal so that internal modules
like pcb_dfm.engine.* can be imported without pulling in extra
dependencies or failing on missing symbols.
"""

from .results import CheckResult, DfmResult

__all__ = [
    "CheckResult",
    "DfmResult",
]
