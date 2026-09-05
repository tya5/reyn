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
    called with, for real subprocess-launch verification.

    Writes ATOMICALLY (write to `sink.tmp`, then `mv` it over `sink` —
    `mv` within one filesystem is a `rename(2)`, atomic) so the caller's
    own `sink.exists()` poll means "the write is COMPLETE", not merely
    "the file was created" — a non-atomic `echo > sink` has a real
    window between create and write-complete that a separate process's
    poll can land inside (CI-observed: `sink.read_text()` sometimes
    empty). This makes existence and completeness the SAME observable
    fact, rather than teaching the poll a smarter wait condition — the
    poll only ever asked one honest question already; the fake process
    was answering it dishonestly mid-write."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "opened.txt"
    sink_tmp = tmp_path / "opened.txt.tmp"
    script = bindir / name
    script.write_text(f'#!/bin/sh\necho "$1" > {sink_tmp}\nmv {sink_tmp} {sink}\n')
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


@pytest.mark.skipif(sys.platform == "win32", reason="darwin/linux opener path only")
def test_fake_opener_sink_does_not_exist_mid_write(tmp_path, monkeypatch):
    """Tier 2: strip-falsifier for the atomic-write fix above. A fake
    opener that pauses BETWEEN writing `sink.tmp` and renaming it into
    `sink` must show `sink.exists() is False` for the whole pause — the
    real, driven witness that atomic write actually closes the race the
    sibling test's own `while not sink.exists()` poll depends on (CI
    observed the pre-fix version pass the existence check with an EMPTY
    file: `AssertionError: assert '' == '/tmp/.../report.pptx'`)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "opened.txt"
    sink_tmp = tmp_path / "opened.txt.tmp"
    pause_marker = tmp_path / "paused"
    resume_marker = tmp_path / "resume"
    opener_name = "open" if sys.platform == "darwin" else "xdg-open"
    script = bindir / opener_name
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$1" > {sink_tmp}\n'
        f"touch {pause_marker}\n"
        f"while [ ! -f {resume_marker} ]; do sleep 0.02; done\n"
        f"mv {sink_tmp} {sink}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    target = tmp_path / "report.pptx"
    target.write_text("fake pptx bytes")

    ok = open_with_os_default(target)
    assert ok is True

    while not pause_marker.exists():  # unbounded — CI's own timeout is the backstop
        time.sleep(0.02)
    # The write to sink.tmp is done, but the rename has not happened yet —
    # this is the exact window the pre-fix non-atomic `echo > sink` did
    # NOT have: `sink` itself must not exist.
    assert sink_tmp.exists()
    assert not sink.exists()

    resume_marker.touch()  # let the opener complete the rename

    while not sink.exists():
        time.sleep(0.02)
    assert sink.read_text().strip() == str(target)


def test_returns_false_when_the_opener_binary_is_missing(monkeypatch, tmp_path):
    """Tier 1: no opener on PATH at all — Popen raises FileNotFoundError,
    caught and reported as False, never propagated to the caller."""
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory, no opener binaries
    ok = open_with_os_default(tmp_path / "whatever.pptx")
    assert ok is False
