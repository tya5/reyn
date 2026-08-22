"""#3362 gates: ``/copy`` and ``/rewind`` are real on the default Textual TUI.

Both commands were silent no-ops there: ``app._SKIP_KINDS`` filtered their
sentinels out of the conversation pane and nothing else consumed them, so
``/copy`` never touched the clipboard and ``/rewind`` never showed a list.

Graded invariants:

1. **``/copy`` changes the clipboard** — witnessed by EFFECT, not by the status
   line: the real ``copy_to_clipboard`` (a thin pyperclip wrapper, #3616 ①) is
   pinned to pyperclip's ``xclip`` backend via pyperclip's own public
   ``set_clipboard("xclip")`` API (portable across the macOS dev host and
   Linux CI — see the ``clipboard`` fixture below for why the backend must be
   pinned rather than left to platform auto-detection), which shells out to a
   bare ``xclip`` argv0 resolved via PATH, so these tests put a REAL
   executable named ``xclip`` on ``PATH`` that records its stdin to a file. The
   assertion is on that file's bytes — a status line saying "copied" while the
   subprocess never ran would FAIL here. (Nothing is mocked: pyperclip's own
   argv and pipe run for real; only which binary answers to ``xclip`` and
   which backend pyperclip is pinned to are arranged.)
2. **Every reply the pane shows is copyable** — including a STREAMED reply (which
   settles through an early return in ``_ingest_frame``) and a reply restored
   from ``history.jsonl``, not only the plain live path.
3. **``/rewind`` lists AND rewinds** — two separable things, gated separately:
   the picker is populated from the command-UI request's points, and PICKING a
   row performs the rewind through the ordinary ``/rewind <seq>`` slash seam.
   The action gate additionally pins that the picker's submission is IDENTICAL
   to a typed one, so the picker cannot grow a private action path beside the
   real one.
4. **Both former sentinels retired** (#4534 PR-2 / PR-2b) — ``/attach`` and
   ``/session switch`` now go through ``ClientTransport.request_attach`` /
   ``request_session_switch``, typed operations; ``app._SKIP_KINDS`` no
   longer has a sentinel entry, and neither kind is even constructible
   (removed from the closed vocabulary). See section 4's own comment below
   for the deleted test this leaves behind.

**No real state is ever rewound here.** The app's rewind action ends at the
``ClientTransport.run_slash_command`` seam (#3595 S5 — a slash the app routes is
run as a command, not submitted as a turn), and the transport under test is a
real but scripted ``ClientTransport`` that records the command instead of
running it — the destructive ``AgentRegistry.checkout`` leg is pinned by its own
existing tests (``test_slash_rewind_1f.py``), not re-driven here.

All app-level tests use real instances (a concrete ``ClientTransport`` + a
concrete ``ChatReadModel`` seam impl + the real app/pilot), per the testing
policy — no mocks.
"""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.rewind_picker import RewindPicker
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage

# ── real seam impls (no mocks) ────────────────────────────────────────────────


class _PickerReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl (the shape ``RegistryReadModel``
    has) that hosts a command-UI region and serves one pending request plus an
    optional persisted history — exactly the two reads the app makes here."""

    @property
    def capabilities(self):
        # #4996: a test double simulating a fully-capable (local-shaped)
        # read model — every accessor above is a REAL, non-degraded
        # implementation for this test's own purposes, not a stand-in for
        # RemoteReadModel's frame-sufficiency boundary.
        return LOCAL_CHAT_READ_CAPABILITIES

    def __init__(
        self,
        pending: "dict | None" = None,
        messages: "list[ChatMessage] | None" = None,
    ) -> None:
        self._pending = pending
        self._messages = list(messages or [])

    def snapshot(self, config=None):
        return None

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return self._pending

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self) -> Path:
        return Path("/tmp/reyn_3362_input_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return self._messages[-limit:] if limit is not None else list(self._messages)

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


class _RemoteishReadModel(_PickerReadModel):
    """The REMOTE shape: command-UI is not on the AG-UI wire, so
    ``pending_command_ui`` is ``None`` and no region is hosted — the same
    decision ``RemoteReadModel`` makes."""

    @property
    def has_command_ui_region(self) -> bool:
        return False


class ScriptedTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport`. The stream stays open after the
    scripted frames so the app stays mounted; ``submitted`` records the lines the
    app routes back through the send seam."""

    def __init__(self, messages: "list[OutboxMessage] | None" = None) -> None:
        self._messages = list(messages or [])
        self.submitted: "list[str]" = []
        # #3595 S5: a slash line the app routes is RUN as a command through this
        # seam; ``submitted`` keeps its meaning of "went out as a turn".
        self.commands: "list[str]" = []
        #: #5045: how many times the app consumed the pending command-UI
        #: request through THIS seam (moved off the read model, which must
        #: not write) — public so a test can assert the consumption without
        #: reaching into private state.
        self.cleared = 0

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def run_slash_command(self, name: str, args: str) -> bool:
        self.commands.append(f"/{name} {args}".rstrip())
        return True

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        self._messages.append(msg)

    async def clear_pending_command_ui(self) -> None:
        self.cleared += 1

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


# ── clipboard EFFECT witness ──────────────────────────────────────────────────


@pytest.fixture()
def clipboard(tmp_path, monkeypatch):
    """Put a REAL ``xclip`` executable on ``PATH`` that records its stdin, and
    force pyperclip to use it — an environment arrangement, not a mock.

    Returns a zero-arg callable giving the recorded text, or ``None`` when the
    binary was never invoked.

    #3616 ①: ``copy_to_clipboard`` is a thin pyperclip wrapper, and
    pyperclip's OWN backend-selection (``determine_clipboard()``) is
    PLATFORM-gated — it only ever tries ``pbcopy`` on Darwin, so a fake
    ``pbcopy`` on PATH is invisible to it on Linux CI (which is why this
    fixture, and its 3 near-duplicates across the TUI test files, all went
    red under #3616 ① on Linux while passing locally on macOS: the PREVIOUS
    ``_clipboard.py`` searched ``shutil.which`` over a fixed tool-name list
    regardless of host OS, so a same-named fake binary worked on any
    platform — pyperclip's is not a name search, it is a platform
    dispatch). ``pyperclip.set_clipboard("xclip")`` is pyperclip's own
    PUBLIC API for pinning the backend explicitly (the same API the
    no-backend-available test in ``test_clipboard_pyperclip_3616.py`` uses
    to force the opposite state) — once pinned, ``init_xclip_clipboard()``'s
    ``Popen(['xclip', ...])`` call is a plain PATH lookup with no OS check of
    its own, so a fake ``xclip`` works identically on macOS and Linux. A
    hermetic stand-in for the system clipboard is required because a CI host
    has none — the real-``pbpaste``/``xclip -o`` witness is a manual
    Test-plan item.
    """
    import pyperclip

    original_copy, original_paste = pyperclip.copy, pyperclip.paste

    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "clipboard.txt"
    script = bindir / "xclip"
    # Written ATOMICALLY (temp + rename): a plain ``> sink`` redirect creates the
    # file EMPTY before ``cat`` writes a byte, so a reader polling for "the sink
    # exists" can observe a half-written clipboard and compare against "". That
    # race made two otherwise-unrelated strips report a spurious failure before
    # it was found — the witness itself has to be race-free for a RED to mean
    # what it says.
    script.write_text(
        "#!/bin/sh\n/bin/cat > " + str(sink) + ".part\n"
        "/bin/mv " + str(sink) + ".part " + str(sink) + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    # PREPENDED, not replaced: shadows any real ``xclip`` (the host's actual
    # clipboard is never touched by these tests) while the rest of PATH stays
    # usable.
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    pyperclip.set_clipboard("xclip")

    def read():
        return sink.read_text() if sink.exists() else None

    try:
        yield read
    finally:
        # pyperclip's chosen backend is a MODULE-level global, not scoped to
        # this test — leaving "xclip" pinned would leak into later tests in
        # the same worker process.
        pyperclip.copy, pyperclip.paste = original_copy, original_paste


def _entries(app: TextualChatApp):
    return list(app.query_one(FlowView).entries)


def _texts(app: TextualChatApp):
    return [e.item.text for e in _entries(app)]


async def _settle(pilot, until=None) -> None:
    """Let the frame pump drain the scripted frames and the UI settle.

    The real sleep is required, not padding: the clipboard write is off-loaded to
    a thread executor (``copy_to_clipboard_async``) and spawns a subprocess, so a
    pure ``pilot.pause()`` loop can return before that thread has finished.

    ``until`` is an optional zero-arg predicate to stop early on. Passing it buys
    a GENEROUS timeout budget for a slow CI host without paying that budget on
    every test — and it never weakens an assertion: the caller still asserts the
    real thing afterwards, so a predicate that never becomes true fails there
    rather than here.
    """
    # A waited-for condition gets a generous budget (only spent when the
    # condition is genuinely slow or never arrives); a plain drain does not need
    # one, since every step it waits on is synchronous.
    for _ in range(150 if until is not None else 12):
        await pilot.pause()
        if until is not None and until():
            return
        await asyncio.sleep(0.01)


# ── 1. /copy changes the clipboard (effect, not appearance) ───────────────────


@pytest.mark.asyncio
async def test_copy_sentinel_writes_the_reply_to_the_clipboard(clipboard) -> None:
    """Tier 2: a ``__copy_last_reply__`` frame after a reply puts THAT reply's
    text on the clipboard, and reports it in the pane.

    Both halves are asserted: the clipboard sink holds the reply text (the
    EFFECT — before #3362 the sink was never written at all), and a status row
    appears (the REPORT). Asserting only the row is the exact insufficiency the
    reporter called out."""
    transport = ScriptedTransport([
        OutboxMessage(kind="agent", text="the answer is 42"),
        OutboxMessage(kind="__copy_last_reply__", text=""),
    ])
    app = TextualChatApp(transport=transport, read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await _settle(pilot, until=lambda: clipboard() is not None)
        assert clipboard() == "the answer is 42", (
            "the system clipboard was not written — /copy is still a no-op"
        )
        # The report is appended AFTER the clipboard write returns, so the
        # write-predicate above can (and does) return before the row lands.
        await _settle(pilot, until=lambda: any("clipboard" in t for t in _texts(app)))
        assert any("clipboard" in t for t in _texts(app)), (
            f"no /copy result reported in the pane: {_texts(app)}"
        )


@pytest.mark.asyncio
async def test_copy_targets_an_older_reply_by_number(clipboard) -> None:
    """Tier 2: ``/copy 2`` copies the reply one turn back, not the newest —
    the ring is ordered, not a single-slot latch."""
    transport = ScriptedTransport([
        OutboxMessage(kind="agent", text="older reply"),
        OutboxMessage(kind="agent", text="newest reply"),
        OutboxMessage(kind="__copy_last_reply__", text="2"),
    ])
    app = TextualChatApp(transport=transport, read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await _settle(pilot, until=lambda: clipboard() is not None)
        assert clipboard() == "older reply"


@pytest.mark.asyncio
async def test_copy_with_nothing_buffered_reports_and_writes_nothing(
    clipboard,
) -> None:
    """Tier 2: with no reply buffered, ``/copy`` explains itself AND leaves the
    clipboard untouched — a decision-enabling message, not a spurious copy."""
    transport = ScriptedTransport([
        OutboxMessage(kind="__copy_last_reply__", text=""),
    ])
    app = TextualChatApp(transport=transport, read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert clipboard() is None, "clipboard written with nothing to copy"
        assert any("no agent reply to copy" in t for t in _texts(app)), _texts(app)


@pytest.mark.asyncio
async def test_streamed_reply_is_copyable(clipboard) -> None:
    """Tier 2: a reply that arrives as a STREAM (deltas + an authoritative
    completion carrying the same ``chain_id``) is copyable.

    The completion settles through an early return in ``_ingest_frame``; buffering
    the text after that return would leave the common case — every streamed
    reply — uncopyable while the plain path still passed."""
    transport = ScriptedTransport([
        OutboxMessage(
            kind="agent", text="streamed answer", meta={"chain_id": "c1"},
        ),
        OutboxMessage(kind="__copy_last_reply__", text=""),
    ])
    app = TextualChatApp(transport=transport, read_model=_PickerReadModel())
    async with app.run_test() as pilot:
        await _settle(pilot, until=lambda: clipboard() is not None)
        assert clipboard() == "streamed answer"


@pytest.mark.asyncio
async def test_restored_reply_is_copyable(clipboard) -> None:
    """Tier 2: a reply restored from ``history.jsonl`` on startup — one the user
    can SEE in the pane — is one ``/copy`` can reach.

    The pane and the command must agree about what exists; a ring fed only by
    live frames would show the reply and then deny it."""
    read_model = _PickerReadModel(messages=[
        ChatMessage(role="user", content="what is it"),
        ChatMessage(role="assistant", content="restored answer"),
    ])
    transport = ScriptedTransport([
        OutboxMessage(kind="__copy_last_reply__", text=""),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot, until=lambda: clipboard() is not None)
        assert clipboard() == "restored answer"


# ── 2. /rewind: the LIST ──────────────────────────────────────────────────────


_POINTS = [
    {"seq": 12, "ts": "2026-07-26T10:00:00", "kind": "turn"},
    {"seq": 9, "ts": "2026-07-26T09:30:00", "kind": "plan-step"},
    {"seq": 4, "ts": "2026-07-26T09:00:00", "kind": "turn"},
]


@pytest.mark.asyncio
async def test_rewind_sentinel_opens_the_picker_with_the_checkpoints() -> None:
    """Tier 2: a ``__rewind_list__`` frame reveals the picker, populated from the
    command-UI request's points, and CONSUMES the request.

    Before #3362 the sentinel was skipped and the request was read by nothing, so
    bare ``/rewind`` produced no visible output at all."""
    read_model = _PickerReadModel({"kind": "rewind", "points": _POINTS})
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="rewind to a checkpoint…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        picker = app.query_one(RewindPicker)
        assert picker.display, "the rewind picker never appeared"
        assert picker.has_points()
        rows = picker.query_one("#rewind-picker-options").option_count
        assert rows == len(_POINTS), f"{rows} rows for {len(_POINTS)} checkpoints"
        assert transport.cleared == 1, (
            "the command-UI request was not consumed — it would replay onto a "
            "later sentinel"
        )


@pytest.mark.asyncio
async def test_rewind_without_a_command_ui_request_falls_back_to_the_text_list(
) -> None:
    """Tier 2: with no structured request (the REMOTE shape — command-UI is not
    on the AG-UI wire), the sentinel's text list is RENDERED rather than
    swallowed.

    Swallowing it there would trade one silent no-op for another: the picker it
    would be waiting for can never arrive. Same two-legged rule the plain client
    applies."""
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="seq 4 · turn"),
    ])
    app = TextualChatApp(transport=transport, read_model=_RemoteishReadModel())
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert not app.query_one(RewindPicker).display
        assert any("seq 4" in t for t in _texts(app)), (
            f"the text fallback was swallowed: {_texts(app)}"
        )


# ── 3. /rewind: the ACTION ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_picking_a_checkpoint_rewinds_through_the_real_slash_seam() -> None:
    """Tier 2: picking a row performs the rewind — and does it through the SAME
    seam a typed ``/rewind <seq>`` uses.

    Two properties, both asserted: (a) picking the SECOND row submits the seq of
    that row (``9``), so the row→action mapping is index-correct and not
    off-by-one onto a different checkpoint; (b) the submitted line is byte-equal
    to what typing ``/rewind 9`` into the composer submits, so the picker has no
    private action path beside the real ``rewind_cmd`` → ``AgentRegistry.checkout``
    one. Fixing the list while the action stayed a no-op is the failure this
    gate exists for.

    No real state is rewound: the action ends at the transport's
    ``run_slash_command`` here (#3595 S5 — a slash the app routes is run as a
    command, not submitted as a turn)."""
    read_model = _PickerReadModel({"kind": "rewind", "points": _POINTS})
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.query_one(RewindPicker).display
        # ↓ moves to the second checkpoint, Enter checks it out.
        await pilot.press("down")
        await pilot.press("enter")
        await _settle(pilot)
        assert transport.commands == ["/rewind 9"], (
            f"picking a checkpoint did not rewind: {transport.commands}"
        )
        # (b) the SAME line a typed submission produces.
        typed = ScriptedTransport()
        typed_app = TextualChatApp(transport=typed, read_model=_PickerReadModel())
        async with typed_app.run_test() as typed_pilot:
            await typed_pilot.pause()
            await typed_app._submit("/rewind 9")
            await typed_pilot.pause()
        assert transport.commands == typed.commands, (
            "the picker's rewind is not the typed /rewind path"
        )
        assert not app.query_one(RewindPicker).display, (
            "the picker stayed open after checking out"
        )


@pytest.mark.asyncio
async def test_escape_dismisses_the_picker_without_rewinding() -> None:
    """Tier 2: Esc closes the picker and submits NOTHING — an accidental bare
    ``/rewind`` must not be a trap in front of a destructive operation."""
    read_model = _PickerReadModel({"kind": "rewind", "points": _POINTS})
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.query_one(RewindPicker).display
        await pilot.press("escape")
        await _settle(pilot)
        assert not app.query_one(RewindPicker).display
        assert transport.commands == [] and transport.submitted == [], (
            f"Esc rewound something: {transport.commands or transport.submitted}"
        )


# #4534 PR-2b retired the last of `_SKIP_KINDS`'s two former sentinel entries
# (`__attach_request__` in PR-2, `__session_switch_request__` here) — neither
# `/attach` nor `/session switch` posts an `OutboxMessage` sentinel anymore
# (both go through `ClientTransport.request_attach`/`request_session_switch`,
# typed operations), and neither kind is constructible at all (removed from
# the closed vocabulary). `_SKIP_KINDS` is now empty; the falsify-verify this
# section's test used to pin (a `__session_switch_request__` frame skipped,
# alongside an ordinary `agent` frame confirmed to still render) confirmed RED
# at construction time — `OutboxMessage(kind="__session_switch_request__", ...)`
# raises `ValueError` (kind not in `VOCABULARY`) — so the test is deleted
# rather than rewritten: there is no longer a sentinel kind left to skip.


# ── environment sanity ────────────────────────────────────────────────────────


def test_clipboard_witness_actually_witnesses(clipboard) -> None:
    """Tier 2: the clipboard fixture is a working witness — the production
    ``copy_to_clipboard`` finds the binary and its stdin lands in the sink.

    Without this, every clipboard assertion above could be passing (or failing)
    for reasons that have nothing to do with the code under test — the
    'is the observation infrastructure capturing what you need?' check.
    """
    from reyn.interfaces.repl._clipboard import copy_to_clipboard

    assert clipboard() is None
    ok = copy_to_clipboard("witness marker")
    assert ok is True, f"clipboard witness not wired: {ok!r}"
    assert clipboard() == "witness marker"
    assert os.environ["PATH"].split(os.pathsep)[0].endswith("bin")
