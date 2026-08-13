"""#3507 — flowview's per-character text cursor, reachable from the
conversation pane (0.7.0 introduced it behind an explicit entry step; 0.13
reworked it into an always-on toggle — #3692 PR-A).

0.6.x had an ENTRY-granular keyboard cursor and nothing finer; "can the cursor
move inside an entry" was a real upstream gap. 0.7.0 filled it with a
per-character text cursor with vim motions, and renamed the old entry cursor
to *highlight* to free the word.

What these tests pin is reyn's WIRING, deliberately not flowview's motions:
that ``c`` shows the cursor, that it starts on the highlighted entry, and that
the addressed-row rail is not disturbed by it. The motions (hjkl w b e 0 $ ^ gg
G v V y …) are flowview's own defaults and its own tests' business —
re-asserting them here would pin someone else's contract and would have to be
rewritten every time upstream tunes a key (owner direction: keep flowview's
default keymap, so reyn declares no motion bindings at all).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.inline.textual_chat.gutter import _MARK_RAIL
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.paths import REPO_ROOT


class _Transport(ClientTransport):
    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass

    async def deliver_pending_answer(self, text: str) -> bool:
        return False


def _railed_rows(flow: FlowView) -> "list[str]":
    return [
        row
        for row in (
            "".join(seg.text for seg in flow.render_line(y))
            for y in range(flow.size.height)
        )
        if _MARK_RAIL in row
    ]


async def _seeded(pilot, app, texts=("older reply", "newest reply")):
    for text in texts:
        app.conversation.append(OutboxMessage(kind="agent", text=text))
    await pilot.pause()
    app.query_one(Composer).focus()
    await pilot.pause()
    await pilot.press("shift+tab")
    await pilot.pause()
    return app.query_one(FlowView)


# test_c_shows_the_cursor_on_the_highlighted_entry removed (#4304, part of #3880):
# per this module's own docstring, reyn declares NO binding for "c" — it falls
# through entirely to flowview's own default keymap. Both asserts (cursor_visible
# flips, current is preserved) pinned flowview's own toggle_cursor behavior, not
# any reyn wiring — if either failed, it would be flowview's bug, not reyn's.


@pytest.mark.asyncio
async def test_the_text_cursor_leaves_the_addressed_row_rail_alone() -> None:
    """Tier 2b: the gutter rail (#3490) still marks the addressed row while the
    text cursor is visible, and still marks the SAME row.

    This is the interaction worth pinning rather than the motions: flowview
    holds the highlight fixed while the cursor is shown and posts no
    ``Highlighted`` while the text cursor moves, which is exactly what the
    rail depends on — it is re-derived from ``Highlighted`` plus focus
    changes. If upstream ever moved the highlight per motion, the rail would
    chase the text cursor and this goes red."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 20)) as pilot:
        flow = await _seeded(pilot, app)
        before = _railed_rows(flow)
        assert any("newest reply" in row for row in before), (
            f"test setup: the addressed row is not railed: {before!r}"
        )

        await pilot.press("c")
        await pilot.pause()
        after = _railed_rows(flow)
        assert any("newest reply" in row for row in after), (
            f"the rail left the addressed row on showing the text cursor: {after!r}"
        )
        assert not any("older reply" in row for row in after), (
            f"a second row became railed with the text cursor shown: {after!r}"
        )


def _binding_surfaces() -> "dict[str, list[str]]":
    """Every class in the package that declares ``BINDINGS``, and the keys it
    claims — DERIVED from the source tree, not hand-listed.

    A hand-written list would reproduce, one level up, the very defect this
    gate exists to catch: the previous version read one of the four surfaces
    while claiming to cover reyn's keymap, and "there are only four today" is
    exactly as weak as "only the app declares bindings today". The derivation
    walks the AST for ``BINDINGS`` assigned in a ``ClassDef`` body, so a new
    surface is covered the day it is written.

    Keys are read in all THREE shapes Textual accepts in a BINDINGS list: a
    bare string, a tuple whose first element is the key, and ``Binding(key,
    ...)``. Missing a shape would make a real binding invisible here, which in
    a gate that asserts an ABSENCE is indistinguishable from compliance.

    Deliberately NOT ``Widget.__subclasses__()``: that only sees classes some
    import has already executed, so a surface nobody imported would silently
    read as "no violations" — the worst direction for this particular check.
    """
    import ast

    package = REPO_ROOT / (
        "src/reyn/interfaces/inline/textual_chat"
    )
    surfaces: dict[str, list[str]] = {}
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    targets = stmt.targets
                elif isinstance(stmt, ast.AnnAssign):
                    targets = [stmt.target]
                else:
                    continue
                if not any(
                    isinstance(t, ast.Name) and t.id == "BINDINGS" for t in targets
                ):
                    continue
                keys: list[str] = []
                for element in getattr(stmt.value, "elts", []):
                    if isinstance(element, ast.Constant) and isinstance(
                        element.value, str
                    ):
                        keys.append(element.value)
                    elif isinstance(element, ast.Tuple) and element.elts:
                        first = element.elts[0]
                        if isinstance(first, ast.Constant) and isinstance(
                            first.value, str
                        ):
                            keys.append(first.value)
                    elif isinstance(element, ast.Call) and element.args:
                        first = element.args[0]
                        if isinstance(first, ast.Constant) and isinstance(
                            first.value, str
                        ):
                            keys.append(first.value)
                surfaces[node.name] = keys
    return surfaces


def test_no_reyn_surface_declares_a_flowview_owned_key() -> None:
    """Tier 2: NO reyn binding surface claims a key flowview's text cursor /
    visual mode owns — the keymap over the conversation content is entirely
    flowview's (owner direction), and reyn adds none of it.

    The surfaces are DERIVED from the tree (see :func:`_binding_surfaces`), so
    this covers a binding surface added tomorrow. An earlier version read only
    ``TextualChatApp.BINDINGS`` and claimed to cover reyn's keymap; the fix for
    that must not be a hand-written list of the four that exist today, which
    would be the same defect one level up.

    #3692 PR-A: ``c`` used to be reyn's one declared exception (the entry key
    into flowview's then-explicit text-cursor mode). flowview 0.13 made the
    text cursor always-on and ``c`` its own key (``toggle_cursor``) — reyn now declares
    NONE of this keymap, ``c`` included, closing the boundary this gate
    exists to hold: it is not enough to have removed reyn's own binding once,
    the guard must also stop the SAME key (or a sibling like ``j``) from
    being re-added to an app-level surface later without anyone noticing."""
    surfaces = _binding_surfaces()

    # Non-vacuity: a derivation that found nothing would pass this gate while
    # checking nothing at all.
    assert surfaces, "no BINDINGS surface was derived — the walk is broken"
    assert "TextualChatApp" in surfaces, (
        f"the app's own bindings were not derived; found {sorted(surfaces)}"
    )

    # flowview's own keymap (`_view.py`'s always-live BINDINGS, #3692's issue
    # body §"flowview 0.13 が既に提供するキー") — every key reyn must never
    # shadow, `c` (the former copy-mode entry key) included now that it is
    # flowview's own `toggle_cursor`, not reyn's. #3692 PR-B ③ resolved the
    # `ctrl+f` conflict by moving reyn's search off it (to `ctrl+n`), so
    # `ctrl+f` joins the vim-scroll set here too. `escape` stays EXCLUDED —
    # not a still-open conflict but the opposite: reyn's own app-level
    # `escape` binding is REQUIRED for the layered Esc design (#3692 PR-B
    # ②, `test_esc_with_an_active_selection_cancels_it_and_stays_on_the_pane`
    # in the esc-sufficiency file), so it would be a false positive here.
    flowview_owned = {
        "h", "j", "k", "l", "w", "b", "e", "0", "$", "^", "g", "G", "[",
        "ctrl+d", "ctrl+u", "ctrl+e", "ctrl+y", "ctrl+b", "ctrl+f",
        "v", "V", "y", "*", "n", "N", "c",
    }
    offenders = {
        name: sorted(set(keys) & flowview_owned)
        for name, keys in surfaces.items()
        if set(keys) & flowview_owned
    }
    assert not offenders, (
        f"reyn surfaces declared a key flowview owns: {offenders} — this "
        "keymap belongs to flowview's text cursor / visual mode and a reyn "
        "binding would shadow it"
    )


@pytest.mark.asyncio
async def test_the_text_cursors_yank_writes_through_reyns_local_clipboard(
    tmp_path, monkeypatch
) -> None:
    """Tier 2b: the text cursor's yank clipboard sink is reyn's local tool, and
    its result is observable.

    flowview's default sink is ``App.copy_to_clipboard`` — OSC 52, which
    Textual's own docstring says does not work on macOS Terminal, which tmux/ssh
    can swallow, and which cannot be acknowledged (so it optimistically reports
    success). reyn passes ``clipboard=`` instead, so ``y`` lands where ``/copy``
    and Enter-on-an-entry already land, and a FAILED yank is distinguishable
    from a successful one.

    Exercised through the public ``write_clipboard`` seam rather than by driving
    motions: the motions are flowview's contract, the sink is reyn's. The tool
    is a REAL ``xclip`` on PATH (environment arrangement, not a mock).

    #3616 ①: ``copy_to_clipboard`` is a thin pyperclip wrapper whose backend
    selection is PLATFORM-gated (pyperclip only tries ``pbcopy`` on Darwin),
    so a same-named fake binary would be invisible to it on Linux CI. The
    backend is pinned explicitly via pyperclip's own public
    ``set_clipboard("xclip")`` API, which makes the fake portable across
    hosts — see ``test_textual_chat_copy_rewind_3362.py``'s ``clipboard``
    fixture for the full rationale."""
    import os
    import stat

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
        app = TextualChatApp(transport=_Transport())
        async with app.run_test(size=(80, 20)) as pilot:
            flow = await _seeded(pilot, app)
            # flowview 0.19.0: write_clipboard became `async def` (its own
            # public method, not the injected clipboard= sink's contract —
            # that stays sync-or-async, see `_write_clipboard`'s own
            # docstring, app.py) so its own caller must await it now.
            wrote = await flow.write_clipboard("yanked via the text cursor")
            assert wrote is True, (
                "the sink reported failure — reyn's clipboard tool did not accept the "
                "text, or the default OSC 52 path is still in use"
            )
            for _ in range(60):
                await pilot.pause()
                if sink.exists():
                    break
            assert sink.exists() and sink.read_text() == "yanked via the text cursor", (
                "the text cursor's yank did not reach reyn's local clipboard tool"
            )
    finally:
        pyperclip.copy, pyperclip.paste = original_copy, original_paste


# #3692 PR-A: test_the_chrome_sees_both_text_cursor_edges retired (clean break,
# CLAUDE.md testing.md § extracted-refactor test lifecycle). It pinned reyn's
# OWN narration of the text cursor's entry/exit via flowview 0.8.0's
# now-removed mode-changed message (``on_flow_view_copy_mode_changed``,
# deleted in this same PR per #3692's removal table) — the status-row text it
# asserted on (an entry-hint line naming the motion keys, and its
# exit counterpart) no longer exists and has no successor in this PR's scope.
# The Esc-layering behavior it incidentally also exercised (first Esc stays
# on the pane, second Esc returns to the composer) is reyn's own, separate
# from the removed narration, and is PR-B's to re-pin properly — #3692's
# acceptance criteria calls for 3 SEPARATE Esc-layer tests there, not one
# bundled with a removed feature's assertions.
