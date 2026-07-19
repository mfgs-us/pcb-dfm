"""
Compatibility shims for the (unmaintained) ``pcb-tools`` / ``gerber`` library.

pcb-tools opens files with the legacy ``"rU"`` universal-newline mode, which was
removed in Python 3.11. Rather than permanently replacing ``builtins.open`` for
the whole host process at import time (the old approach — global, applied even
when the import failed, and never restored), we scope the translation to the
short window in which pcb-tools actually runs, via a self-restoring context
manager.

Longer term the intent is to migrate off pcb-tools (e.g. to ``gerbonara``,
which is actively maintained and needs no such shim); until then this keeps the
hack contained.

Note: like the original, this mutates ``builtins.open`` for the duration of the
``with`` block, so it is not safe against other threads opening files
concurrently. The engine runs checks sequentially, so this is acceptable and is
strictly narrower than the previous always-on global patch.
"""

from __future__ import annotations

import builtins
from contextlib import contextmanager


@contextmanager
def rU_open_shim():
    """Translate legacy ``"rU"``/``"U"`` file modes to ``"r"`` for the duration
    of the block, then restore the real ``builtins.open``."""
    saved_open = builtins.open

    def _shimmed_open(file, mode="r", *args, **kwargs):
        if isinstance(mode, str) and "U" in mode:
            mode = mode.replace("U", "") or "r"
        return saved_open(file, mode, *args, **kwargs)

    builtins.open = _shimmed_open
    try:
        yield
    finally:
        builtins.open = saved_open
