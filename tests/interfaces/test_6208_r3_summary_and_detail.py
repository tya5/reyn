"""Tier 2: #6208 R3 — the summary line always fits in ONE line, and the
Space detail view now shows a call's FULL argument text alongside its
FULL result text (the "呼び出し単位" design, architect + lead-coder
final ruling, issue #6208 thread — verbatim: "概要 = tool <subject…>
(k=v, …) を 1 行... 詳細 = Space・呼び出し単位 で 引数の全文 ＋ 結果の
全文").

## Background — accept ④'s own withdrawal

#6184 段3-3 landed accept ④: the tool-call summary's ``subject`` half
was never length-cut ("the display's CENTER... would be defeated by a
subject that could itself be cut mid-string"). A real ``TextualChatApp``
measurement (issue #6208, artifact ``636a2032``) found this made
``exec``'s own summary line wrap to 5 lines at width 80 — the OPPOSITE
of the owner's "情報量がスカスカ" complaint, but a real defect in its
own right (a 1-call summary should not occupy 5 lines of scrollback).
Architect withdrew accept ④ once R3's own detail view gave the full
text somewhere else to be read in full.

## Accept criteria (issue #6208, lead-coder's dispatch, effect-based)

① `exec`'s summary line fits in ONE line at width 80.
② A truncated subject carries its own `…` (the accept ④ reversal — see
   ``tests/interfaces/test_6184_stage3_3_subject_display.py``'s own
   ``test_tool_head_width_overflow_cuts_the_subject_too_now`` for the
   header-level unit witness; this file covers the end-to-end shape).
③ `network` (the permission axis) never disappears from the summary
   line — covered at the unit level in the sibling file above
   (``test_tool_head_width_overflow_never_drops_a_kv_pair_network_
   survives``); this file adds the end-to-end version.
④ Space on a settled row shows the full argument text (today: result
   only).
⑤ No new operation — Space is still the only key that opens tool
   detail (grep witness: exactly one ``Binding("space", ...)`` in
   ``app.py``, unchanged by this stage).
⑥ The existing "expand result" behavior is not broken (deny side —
   the result half keeps showing, unaffected by the new args half).

Real ``TextualChatApp`` + ``FlowView``, real producer path
(``_compose_args``/``resolve_tool_subject``/``render_subject``, the
SAME 3 functions ``lifecycle_forwarder.py`` calls in production) — no
mocks, per the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from rich.console import Console
from textual_flowview import FlowView

from reyn.core.present.tool_call_compose import render_subject
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat._meta_keys import EXPANDED_KEY as _EXPANDED_KEY
from reyn.interfaces.repl.renderer import _compose_args
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.tools.subject import resolve_tool_subject
from tests._support.paths import REPO_ROOT


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time —
    same shape as the sibling #4691/#6198 test files' own local copy
    (neither module exports its collaborator)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:  # pragma: no cover
        return "msg-1"

    async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:  # pragma: no cover
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


def _started(tool: str, args: dict, *, op_id: str = "op-1") -> OutboxMessage:
    """Drives the SAME 3 producer functions ``lifecycle_forwarder.py``
    calls in production (``_compose_args``, ``resolve_tool_subject``,
    ``render_subject``) — a real producer path, not a hand-built
    ``details``/``subject`` shortcut."""
    composed = _compose_args(args)
    raw_subject = resolve_tool_subject(tool, args)
    subject = render_subject(raw_subject) if raw_subject is not None else None
    return OutboxMessage(
        kind="tool_call_started", text=tool,
        meta={"tool": tool, "op_id": op_id, "dispatch_id": op_id, "args": args, "call_id": None},
        subject=subject,
        details={"args": composed},
    )


def _completed(tool: str, result: object, *, op_id: str = "op-1") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_completed", text="",
        meta={"tool": tool, "op_id": op_id, "dispatch_id": op_id, "call_id": None, "result": result},
    )


async def _plain_async(app: TextualChatApp, entry, width: int) -> str:
    pres = await app._presenter.present(entry, width)
    console = Console(width=width, no_color=True)
    with console.capture() as cap:
        console.print(pres.renderable)
    return cap.get()


_EXEC_CMD = "git log --oneline --graph --decorate --all --since='2 weeks ago'"


@pytest.mark.asyncio
async def test_execs_summary_line_fits_in_one_line_at_width_80() -> None:
    """Tier 2: accept① — the real, owner-reported regression case: a long
    ``cmd`` with several other args. Before this stage the subject stayed
    uncut and this row wrapped to 5 physical lines at width 80."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(
            _started("exec", {"cmd": _EXEC_CMD, "timeout": 120, "network": True, "collect": "both"})
        )
        await pilot.pause()
        await transport.push_display(
            _completed("exec", {"status": "ok", "exit_code": 0, "stdout": "abc\n", "stderr": ""})
        )
        await pilot.pause()
        flow = app.query_one(FlowView)
        (entry,) = flow.entries
        pres = await app._presenter.present(entry, 80)
        # height 2: one line for the (now-cut) summary, one for `⎿ ok` —
        # not the pre-stage 5+ lines a real render produced.
        assert pres.height == 2, (
            f"expected the settled row (summary + ⎿ result) to take "
            f"exactly 2 lines at width 80, got {pres.height}"
        )


@pytest.mark.asyncio
async def test_execs_summary_line_never_drops_network_end_to_end() -> None:
    """Tier 2: accept③ end to end — the permission axis (`network`)
    survives the SAME long-`cmd` composition that triggers the subject
    cut above."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(
            _started("exec", {"cmd": _EXEC_CMD, "timeout": 120, "network": True, "collect": "both"})
        )
        await pilot.pause()
        await transport.push_display(_completed("exec", {"status": "ok"}))
        await pilot.pause()
        flow = app.query_one(FlowView)
        (entry,) = flow.entries
        text = await _plain_async(app, entry, 80)
        assert "network=True" in text


@pytest.mark.asyncio
async def test_space_on_a_settled_row_shows_the_full_argument_text() -> None:
    """Tier 2: accept④ — the PR's own "body". Setting `_EXPANDED_KEY`
    (the SAME flag `action_toggle_fold`/Space always used, unchanged)
    on a settled row whose summary was cut must now show the FULL,
    uncut `cmd` text — unreachable before this stage (only the result
    used to expand). Strip-falsify witness: removing the new args block
    (reverting presenter.py's own wiring) turns this red."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(
            _started("exec", {"cmd": _EXEC_CMD, "timeout": 120, "network": True, "collect": "both"})
        )
        await pilot.pause()
        await transport.push_display(
            _completed("exec", {"status": "ok", "exit_code": 0, "stdout": "abc\n", "stderr": ""})
        )
        await pilot.pause()
        flow = app.query_one(FlowView)
        (entry,) = flow.entries
        entry.item.meta[_EXPANDED_KEY] = True
        text = await _plain_async(app, entry, 80)
        assert _EXEC_CMD in text, (
            "the full, uncut command must be visible somewhere in the "
            "expanded row — it is cut in the summary by design (accept①)"
        )
        assert "timeout=120" in text
        assert "collect=both" in text


@pytest.mark.asyncio
async def test_space_still_shows_the_full_result_alongside_the_args() -> None:
    """Tier 2: accept⑥ (deny side) — expanding must not TRADE the
    existing result detail away for the new args detail; both halves
    of the call are present together (architect's own "呼び出し単位"
    framing: the call and its result expand TOGETHER, not one
    replacing the other)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(
            _started("exec", {"cmd": _EXEC_CMD, "timeout": 120, "network": True, "collect": "both"})
        )
        await pilot.pause()
        await transport.push_display(
            _completed("exec", {"status": "ok", "exit_code": 0, "stdout": "unique-stdout-marker", "stderr": ""})
        )
        await pilot.pause()
        flow = app.query_one(FlowView)
        (entry,) = flow.entries
        entry.item.meta[_EXPANDED_KEY] = True
        text = await _plain_async(app, entry, 80)
        assert _EXEC_CMD in text, "the args half must be present"
        assert "unique-stdout-marker" in text, "the result half must ALSO still be present"


@pytest.mark.asyncio
async def test_expanding_a_row_whose_summary_was_never_cut_shows_no_redundant_args_block() -> None:
    """Tier 2: the deny-side sibling of accept④ — a short call whose
    summary already shows everything must not print the same args
    twice when expanded (the exact "printed the same sentence twice"
    class of real-terminal-only bug `_tool_result_line`'s own analogous
    guard, for the result half, already exists to prevent — #3508's own
    docstring)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_started("exec", {"cmd": "echo hi", "network": True}))
        await pilot.pause()
        await transport.push_display(_completed("exec", {"status": "ok"}))
        await pilot.pause()
        flow = app.query_one(FlowView)
        (entry,) = flow.entries
        entry.item.meta[_EXPANDED_KEY] = True
        text = await _plain_async(app, entry, 80)
        assert "args:" not in text, (
            "nothing was cut in the summary -- an args detail block "
            "here would only repeat it"
        )


def test_space_is_still_the_only_key_that_opens_tool_detail() -> None:
    """Tier 2: accept⑤ — no new operation was added. Grep witness
    (matches this arc's own established census style, e.g. #6193's
    accept ⑥ single-call-site check): app.py's real ``BINDINGS``
    declaration still names ``toggle_fold`` as the ONE action bound to
    ``"space"`` — the SAME action this stage's own detail view rides
    on. A future new operation (a second key, or ``"space"`` rebound to
    a different action) would drop this exact substring."""
    app_py = (REPO_ROOT / "src" / "reyn" / "interfaces" / "inline"
              / "textual_chat" / "app.py").read_text(encoding="utf-8")
    # The real binding declaration's own exact text -- distinct from
    # app.py:487's docstring, which quotes upstream's DIFFERENT original
    # action name (``Binding("space", "activate")``), so no separate
    # docstring-exclusion logic is needed: the two strings do not
    # collide with each other at all.
    assert 'Binding("space", "toggle_fold"' in app_py, (
        "expected app.py's real BINDINGS list to still bind \"space\" to "
        "toggle_fold -- a new/changed binding here would mean a new "
        "operation, which #6208 R3 explicitly does not add"
    )
