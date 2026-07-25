"""The Gerber parse cache must speed up re-reads without serving stale data.

A single run parses the same Gerber several times -- the geometry build reads
each layer, then trace/edge checks re-read the copper and outline. Those parses
are memoized by (path, mtime). This pins the two properties that memoization
must not get wrong: the same file returns an identical result, and a file whose
content changes (new mtime) is re-parsed rather than served from the cache.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("gerbonara", reason="gerbonara not installed")

from pcb_dfm.geometry.gerber_backend import gerber_traces_mm  # noqa: E402

_ONE_TRACE = (
    "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,{w:.6f}*%\nD10*\n"
    "X1000000Y1000000D02*\nX9000000Y1000000D01*\nM02*\n"
)


def test_same_file_returns_an_identical_result(tmp_path):
    p = tmp_path / "board.gtl"
    p.write_text(_ONE_TRACE.format(w=0.25))

    first = gerber_traces_mm(p)
    second = gerber_traces_mm(p)
    assert len(first) == len(second) == 1
    assert first[0].width_mm == pytest.approx(0.25)
    # A cache hit returns the very same object, which is what makes it free.
    assert first is second


def test_a_changed_file_is_reparsed_not_served_stale(tmp_path):
    """The one thing mtime keying must get right: rewrite the file with a
    different width and a newer mtime, and the parse must reflect the change."""
    p = tmp_path / "board.gtl"
    p.write_text(_ONE_TRACE.format(w=0.25))
    assert gerber_traces_mm(p)[0].width_mm == pytest.approx(0.25)

    # Rewrite with a wider trace and force a distinctly newer mtime.
    p.write_text(_ONE_TRACE.format(w=0.80))
    future = time.time() + 5
    os.utime(p, (future, future))

    assert gerber_traces_mm(p)[0].width_mm == pytest.approx(0.80), (
        "a modified file must be re-parsed, not returned from the cache"
    )
