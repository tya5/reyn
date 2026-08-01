"""#3616 ①: `_clipboard.py` is a thin pyperclip wrapper, not a hand-rolled
pbcopy/wl-copy/xclip/xsel/clip dispatch table.

Two things this module has to keep true, both from the removed helper's
contract:

1. **Failure stays observable** — pyperclip raises ``PyperclipException`` when
   no backend is available (its own documented behaviour, exercised here via
   its public ``set_clipboard("no")`` API — a real, supported way to force
   that state, not a stand-in for pyperclip). That exception (or any other a
   backend raises) must become ``False``, never propagate, and never read as
   success.
2. **A real copy still succeeds** — a REAL ``xclip`` script on ``PATH``
   records what reaches it, the same environment-arrangement technique
   ``test_textual_chat_copy_rewind_3362.py`` uses for the same reason (no
   system clipboard on a CI host). The backend is pinned explicitly via
   pyperclip's own public ``set_clipboard("xclip")`` API rather than left to
   auto-detection: pyperclip's ``determine_clipboard()`` is PLATFORM-gated
   (it only ever tries ``pbcopy`` on Darwin), so a fake binary keyed to one
   auto-detected backend name is invisible to it on a different host OS —
   pinning the backend is what makes the fake portable between the macOS dev
   host and Linux CI.

Tier 1 (Contract): both are ``copy_to_clipboard``'s public return-value
contract, not an internal detail.
"""
from __future__ import annotations

import os
import stat

import pytest

from reyn.interfaces.repl._clipboard import copy_to_clipboard, copy_to_clipboard_async


@pytest.fixture()
def restore_pyperclip_backend():
    """pyperclip's chosen backend lives in module-level globals
    (``pyperclip.copy`` / ``pyperclip.paste``), set once by
    ``determine_clipboard()`` or ``set_clipboard()`` and otherwise persisting
    for the life of the process — including across other test modules in the
    same session. Forcing a non-default backend for one test would otherwise
    leak into every later clipboard test. Save + restore around the test."""
    import pyperclip

    original_copy, original_paste = pyperclip.copy, pyperclip.paste
    try:
        yield
    finally:
        pyperclip.copy, pyperclip.paste = original_copy, original_paste


@pytest.fixture()
def fake_xclip(tmp_path, monkeypatch, restore_pyperclip_backend):
    """Put a REAL ``xclip`` executable on ``PATH``, recording its stdin, and
    pin pyperclip to it via the public ``set_clipboard("xclip")`` API.
    Returns a zero-arg callable giving the recorded text, or ``None``."""
    import pyperclip

    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "clip.txt"
    script = bindir / "xclip"
    script.write_text(
        "#!/bin/sh\n/bin/cat > " + str(sink) + ".part\n"
        "/bin/mv " + str(sink) + ".part " + str(sink) + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    pyperclip.set_clipboard("xclip")

    def read():
        return sink.read_text() if sink.exists() else None

    return read


def test_no_backend_available_returns_false_not_an_exception(
    restore_pyperclip_backend,
) -> None:
    """Tier 1: pyperclip's own "no clipboard mechanism found" state maps to
    ``ok=False`` — it must not propagate ``PyperclipException`` and must not
    silently read as success.

    Falsification (recorded in the PR body): temporarily changing the
    ``except Exception: return False`` branch in ``copy_to_clipboard`` to
    ``return True`` turns this RED — confirming the assertion actually
    exercises the mapped-failure branch rather than passing vacuously."""
    import pyperclip

    pyperclip.set_clipboard("no")
    assert copy_to_clipboard("anything") is False


def test_real_copy_succeeds_and_is_observable(fake_xclip) -> None:
    """Tier 1: with a real clipboard tool on PATH, ``copy_to_clipboard``
    returns ``True`` AND the text actually reaches the tool — the success half
    of the same contract the no-backend test above falsifies the failure half
    of."""
    ok = copy_to_clipboard("hello from #3616")
    assert ok is True
    assert fake_xclip() == "hello from #3616"


@pytest.mark.asyncio
async def test_async_variant_returns_the_same_bool_shape(fake_xclip) -> None:
    """Tier 1: the async off-load wrapper returns the same plain ``bool`` the
    sync function does — the old ``(ok, tool_label)`` tuple is gone from both
    entry points, not just the sync one."""
    ok = await copy_to_clipboard_async("async path")
    assert ok is True
    assert fake_xclip() == "async path"
