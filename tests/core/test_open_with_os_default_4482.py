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

    result = open_with_os_default(target)
    assert result.state == "accepted"

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
    body = (
        f'echo "$1" > {sink_tmp}\n'
        f"touch {pause_marker}\n"
        f"while [ ! -f {resume_marker} ]; do sleep 0.02; done\n"
        f"mv {sink_tmp} {sink}\n"
    )
    if sys.platform == "darwin":
        # #6224: this module now waits on `open` itself (never on the app
        # it launches — real `open` always forks and returns quickly). A
        # faithful fake `open` must do the same: fork the actual work into
        # the background and exit 0 immediately, or this test would hang
        # inside `open_with_os_default`'s own `communicate()`.
        # Redirect the backgrounded subshell's own stdout/stderr away from
        # the inherited pipe — otherwise it keeps that pipe's write end
        # open after the parent script exits, and `communicate()`'s own
        # `stderr.read()` blocks on EOF until the background job finishes
        # (defeating the fork-and-return-quickly premise this test exists
        # to exercise).
        script.write_text(f"#!/bin/sh\n( {body} ) >/dev/null 2>&1 &\nexit 0\n")
    else:
        script.write_text(f"#!/bin/sh\n{body}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    target = tmp_path / "report.pptx"
    target.write_text("fake pptx bytes")

    result = open_with_os_default(target)
    assert result.state == "accepted"

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


def test_returns_failed_when_the_target_does_not_exist(tmp_path):
    """Tier 1: #6224 — the target file does not exist. Caught by the
    pre-launch existence check (state ``"failed"``, a DEFINITE known
    failure) rather than reaching the launcher at all, on every platform —
    the launcher never even sees a nonexistent target."""
    result = open_with_os_default(tmp_path / "does-not-exist.pptx")
    assert result.state == "failed"
    assert "does not exist" in (result.detail or "")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only rc/stderr branch")
def test_darwin_opener_nonzero_exit_reports_failed_with_the_real_stderr(tmp_path, monkeypatch):
    """Tier 2: #6224 — on macOS ONLY, this module waits on the launcher
    (never the application it starts — see module docstring) and reports a
    non-zero exit as ``"failed"`` with the REAL stderr text, rather than
    the pre-fix behaviour of reporting `open` exiting non-zero as success."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "open"
    script.write_text(
        '#!/bin/sh\necho "No application knows how to open the file." 1>&2\nexit 1\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    target = tmp_path / "report.pptx"
    target.write_text("fake pptx bytes")

    result = open_with_os_default(target)
    assert result.state == "failed"
    assert "No application knows how to open" in (result.detail or "")


@pytest.mark.skipif(
    sys.platform in ("darwin", "win32"), reason="xdg-open-specific non-wait property"
)
def test_linux_opener_is_never_waited_on(tmp_path, monkeypatch):
    """Tier 2: #6224's ② — the DUAL of the darwin test above: on Linux this
    module must return WITHOUT waiting for ``xdg-open`` to exit, because a
    real ``xdg-open`` can stay attached to a terminal-based handler and
    never return at all (electron/electron#10902). A fake opener that
    blocks until a resume marker appears must not make this call block —
    if it did, this test would hang until CI's own --timeout kills it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    resume_marker = tmp_path / "resume"
    script = bindir / "xdg-open"
    script.write_text(
        f"#!/bin/sh\nwhile [ ! -f {resume_marker} ]; do sleep 0.02; done\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    target = tmp_path / "report.pptx"
    target.write_text("fake pptx bytes")

    result = open_with_os_default(target)  # must return immediately
    assert result.state == "accepted"
    resume_marker.touch()  # let the still-running fake opener exit, cleanup


def test_returns_failed_when_the_opener_binary_is_missing(monkeypatch, tmp_path):
    """Tier 1: #6224 — no opener on PATH at all. The pre-launch
    ``shutil.which`` check catches this deterministically (state
    ``"failed"``) before ever attempting ``Popen`` — a real target, just
    no opener to hand it to."""
    target = tmp_path / "whatever.pptx"
    target.write_text("real bytes, so only the opener-missing branch fires")
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory, no opener binaries
    result = open_with_os_default(target)
    assert result.state == "failed"
    assert "not found on PATH" in (result.detail or "")
