"""Tier 2: a yank whose clipboard write fails SAYS SO (#3616).

``_write_clipboard`` returns a bool, and the sink contract has one — but nothing
upstream reads it. flowview's ``action_yank`` calls ``yank()`` and discards the
value, so a bool alone reaches no one. Before this, a yank on a machine with no
clipboard backend was indistinguishable from a yank that worked: the user
presses ``y``, the selection clears, and the clipboard silently still holds
whatever it held before.

That is the path #3616's own acceptance test runs down. The operator's report
came through copy mode (``c`` -> ``y``), not the entry copy — and the entry copy
is the one that already reports both outcomes. Two call sites, one sink, one of
them mute, and the mute one is the one being tested by hand on a Windows shell
where a missing backend is a live possibility.

The failure is REAL, not simulated: ``pyperclip.set_clipboard("no")`` is
pyperclip's own public API for "no backend available", the same technique
``test_clipboard_pyperclip_3616.py`` uses to exercise the same state. Nothing
about reyn's own code is patched — the app, the sink and the message pump are
all the production ones.
"""
from __future__ import annotations

import os
import stat

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from tests.test_textual_chat_copy_rewind_3362 import (
    ScriptedTransport,
    _PickerReadModel,
    _texts,
)


@pytest.fixture()
def no_clipboard_backend():
    """Force pyperclip into its documented no-backend state, and restore.

    The chosen backend lives in pyperclip's module-level globals and otherwise
    persists for the life of the process, so leaving it set would follow this
    test into every later one in the same session.
    """
    import pyperclip

    original_copy, original_paste = pyperclip.copy, pyperclip.paste
    pyperclip.set_clipboard("no")
    try:
        yield
    finally:
        pyperclip.copy, pyperclip.paste = original_copy, original_paste


@pytest.mark.asyncio
async def test_a_failed_yank_reports_instead_of_going_quiet(no_clipboard_backend):
    """Tier 2: with no clipboard backend, the yank sink puts a failure line in
    the conversation rather than returning a bool nobody reads."""
    app = TextualChatApp(transport=ScriptedTransport([]), read_model=_PickerReadModel())

    async with app.run_test() as pilot:
        await pilot.pause()
        ok = app._write_clipboard("何かをコピーした")
        await pilot.pause()

        assert ok is False, "the sink reported success with no backend available"
        assert any("clipboard copy failed" in t for t in _texts(app)), (
            f"the failed yank said nothing — the user cannot tell it from a "
            f"successful one: {_texts(app)}"
        )


@pytest.fixture()
def working_clipboard(tmp_path, monkeypatch):
    """A REAL ``xclip`` on ``PATH`` recording its stdin, with pyperclip pinned to
    it via the public ``set_clipboard`` API.

    Same arrangement as ``test_clipboard_pyperclip_3616.py``'s ``fake_xclip``,
    and for the same two reasons: CI hosts have no system clipboard, and
    pyperclip's auto-detection is PLATFORM-gated (it only tries ``pbcopy`` on
    Darwin), so pinning the backend is what makes one fake binary work on both
    the macOS dev host and Linux CI. Duplicated rather than imported — a pytest
    fixture in another test module is not importable, and moving it to a
    conftest would widen its blast radius to every test in the tree for the sake
    of one reuse.
    """
    import pyperclip

    original_copy, original_paste = pyperclip.copy, pyperclip.paste
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
    try:
        yield lambda: sink.read_text() if sink.exists() else None
    finally:
        pyperclip.copy, pyperclip.paste = original_copy, original_paste


@pytest.mark.asyncio
async def test_a_successful_yank_stays_quiet(working_clipboard):
    """Tier 2: success adds no line.

    The selection clearing is already the acknowledgement, and ``y`` is the one
    action a user repeats — a "copied" line per yank would push the conversation
    up on every press. This is the half a report-everything implementation gets
    wrong, and the failure test above cannot see it.
    """
    app = TextualChatApp(transport=ScriptedTransport([]), read_model=_PickerReadModel())

    async with app.run_test() as pilot:
        await pilot.pause()
        before = list(_texts(app))
        ok = app._write_clipboard("copied fine")
        await pilot.pause()

        assert ok is True, "the real backend did not accept the text"
        assert _texts(app) == before, f"a successful yank added a line: {_texts(app)}"
