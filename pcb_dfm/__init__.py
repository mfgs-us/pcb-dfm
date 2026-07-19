"""
pcb_dfm package init.

For now we keep the public API minimal so that internal modules
like pcb_dfm.engine.* can be imported without pulling in extra
dependencies or failing on missing symbols.
"""

import logging

from .results import CheckResult, DfmResult

# Library convention: attach a NullHandler so importing pcb_dfm never emits log
# output on its own. Applications (and the CLI) opt in by configuring handlers.
logging.getLogger("pcb_dfm").addHandler(logging.NullHandler())

__all__ = [
    "CheckResult",
    "DfmResult",
]
