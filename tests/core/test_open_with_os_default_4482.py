"""Tier 1/2: #4482 PR-3 — `open_with_os_default` (`interfaces/repl/
_open_with_os_default.py`), the OS-opener dispatch.

Real subprocess launch via a fake `open`/`xdg-open` binary on PATH (same
technique `test_copy_mode_3507.py`'s clipboard tests use) — proves the
REAL platform-selection branch fires and REAL argv reaches a real
subprocess, not a claim about the function's own internal logic in
isolation."""
from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from reyn.interfaces.repl._open_with_os_default import open_with_os_default


def _install_fake_opener(tmp_path, monkeypatch, *, name: str) -> Path:
    """A fake `open`/`xdg-open` on PATH that records the path it was
    called with, for real subprocess-launch verification."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "opened.txt"
    script = bindir / name
    script.write_text(f"#!/bin/sh\necho \"$1\" > {sink}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    return sink


@pytest.mark.skipif(sys.platform == "win32", reason="darwin/linux opener path only")
def test_real_subprocess_launch_reaches_the_target_path(tmp_path, monkeypatch):
    """Tier 2: the actual platform opener (`open` on darwin, `xdg-open`
    elsewhere) is invoked with the real target path — a real subprocess
    launch, verified via a fake binary on PATH that writes what it
    received."""
    opener_name = "open" if sys.platform == "darwin" else "xdg-open"
    sink = _install_fake_opener(tmp_path, monkeypatch, name=opener_name)
    target = tmp_path / "report.pptx"
    target.write_text("fake pptx bytes")

    ok = open_with_os_default(target)
    assert ok is True

    while not sink.exists():  # unbounded — CI's own timeout is the backstop
        time.sleep(0.05)
    assert sink.read_text().strip() == str(target)


def test_returns_false_when_the_opener_binary_is_missing(monkeypatch, tmp_path):
    """Tier 1: no opener on PATH at all — Popen raises FileNotFoundError,
    caught and reported as False, never propagated to the caller."""
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory, no opener binaries
    ok = open_with_os_default(tmp_path / "whatever.pptx")
    assert ok is False
