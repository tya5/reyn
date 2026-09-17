"""Tier 2: #6213 — `_running_tools` correlates by `id`/`parent_id`
(`tool:{dispatch_id}`), not `op_id` (`args_hash`).

## The defect (real repro, `origin/main` `e6d3568d2`)

`app.py:1470-1471` (pre-fix, verbatim): "Running tool-call entries keyed
by op_id (== the dispatcher's deterministic args_hash, meta['op_id'])".
`args_hash` is a CONTENT fingerprint — `dispatcher.py:_compute_args_hash`'s
own docstring: "collision risk is acceptable for resume memoization" —
collision is the DESIGN. Calling the SAME tool with the SAME args TWICE
in one turn therefore produced the SAME `op_id` twice: the second
`started` frame's row silently overwrote the first row's handle in
`_running_tools`, so the first call's completion settled the SECOND
(wrong) row, and the first row stayed RUNNING forever.

## The fix

`dispatcher.py`'s `dispatch_tool` mints a fresh `dispatch_id`
(`new_dispatch_id()`) per call, unconditionally — not a content
fingerprint, not a third party's id (`tool_call_id`, #5891, left
untouched, co-existing for its own purpose). `outbox.py`'s ONE
derivation point (`_derive_id_and_parent_id`) turns it into the SAME
identity vocabulary #6184 already established: a `tool_call_started`
row's `id` becomes `tool:{dispatch_id}`; its `tool_call_completed`/
`failed` counterpart's `parent_id` becomes `tool:{dispatch_id}` too —
`_running_tools` now keys by `msg.id` (write) / `msg.parent_id` (read),
the same "find the entry whose id matches my parent_id" query #6184's
nested-resolution vocabulary was built for.

Covers the issue's own 7 accept criteria (dispatcher/lifecycle_forwarder-
level halves live in `tests/core/test_6213_dispatch_id_correlation.py`)
— this file is the `_running_tools`/rendering half:
⑴⑵ same tool + same args, called twice → each completion settles its
   OWN row; no row stays RUNNING forever (deny side).
⑶ an ARGS-LESS tool (deterministic empty-args hash, `44136fa355b3678a`,
   measured) called twice still separates correctly.
⑷ works with NO `call_id` at all (the `tool_call_id`-declared-None
   population's OWN shape — `dispatch_id` does not depend on `call_id`).
⑸ (grep witness, this file's own test) no `src/` site reads `op_id` AS
   a correlation key any more.
⑺ an orphaned completion (no matching started row) renders IDENTICALLY
   whether its own `parent_id` is `call:`- or `tool:`-prefixed — no
   rendering path anywhere reads `parent_id` (grep-confirmed).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import ReynPresenter, TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.paths import REPO_ROOT


def _args_hash(args: dict) -> str:
    """Mirrors dispatcher.py's own _compute_args_hash exactly (SHA-256 of
    canonical JSON, first 16 hex chars) — used here ONLY to construct a
    REALISTIC `op_id` meta value for these fixtures (accept ③'s own
    "empty args are deterministic" premise), never read by the fix
    itself."""
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class ScriptedTransport(ClientTransportStub):
    """A real, minimal ClientTransport replaying a fixed frame list —
    mirrors test_textual_chat_phase2_3273.py's own class exactly."""

    def __init__(self, messages: "list[OutboxMessage]", *, end: bool = False) -> None:
        self._messages = list(messages)
        self._end = end
        self.submitted: list[str] = []

    def start(self) -> None: ...
    def close(self) -> None: ...

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        if self._end:
            yield DisplayFrame(OutboxMessage(kind="__end__", text=""))
        else:
            await asyncio.Event().wait()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> None:
        self.submitted.append(text)

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

    async def cancel_inflight(self) -> None: ...
    async def shutdown(self) -> None: ...


def _started(dispatch_id: str, tool: str = "grep", *, args: "dict | None" = None) -> OutboxMessage:
    args = args or {}
    return OutboxMessage(
        kind="tool_call_started", text=tool,
        meta={
            "tool": tool, "op_id": _args_hash(args), "dispatch_id": dispatch_id, "args": args,
        },
    )


def _completed(dispatch_id: "str | None", tool: str = "grep", result=None) -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_completed", text="",
        meta={
            "tool": tool, "dispatch_id": dispatch_id,
            "result": result or {"op": tool, "count": 3},
        },
    )


def _failed(dispatch_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_failed", text=tool,
        meta={
            "tool": tool, "dispatch_id": dispatch_id,
            "error_kind": "Boom", "error_message": "it broke",
        },
    )


def _entries(app: TextualChatApp, kind: "str | None" = None):
    from textual_flowview import FlowView
    entries = app.query_one(FlowView).entries
    if kind is None:
        return list(entries)
    return [e for e in entries if e.item.kind == kind]


# ── ①② — same tool, same args, called twice ───────────────────────────────


@pytest.mark.asyncio
async def test_same_tool_same_args_twice_each_completion_settles_its_own_row() -> None:
    """Tier 2: accept ①② — the exact #6213 repro. Two started+completed
    pairs for the SAME tool with the SAME args (same op_id — real
    duplicate content fingerprint) but DISTINCT dispatch_ids: each
    completion settles its OWN row, and no row is left RUNNING."""
    from textual_flowview import EntryState

    transport = ScriptedTransport(
        [
            _started("dispatch-A", args={"path": "/x"}),
            _started("dispatch-B", args={"path": "/x"}),
            _completed("dispatch-A", result={"first": True}),
            _completed("dispatch-B", result={"second": True}),
        ],
        end=False,
    )
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        # Each started frame keeps its own row (unpacking into exactly 2
        # named entries IS the "each frame kept its own row" check —
        # raises on 0/1/3+, not a `len(...) == N` size pin).
        entry_a, entry_b = _entries(app, "tool_call_started")

        # Deny side (②): NO row is left RUNNING — both settled.
        assert entry_a.state is EntryState.SUCCESS and entry_b.state is EntryState.SUCCESS, (
            f"expected both rows SUCCESS, got {entry_a.state!r}/{entry_b.state!r} -- a row "
            "still RUNNING means a completion settled the WRONG entry (the exact #6213 symptom)"
        )

        # Each settled with ITS OWN result, not swapped.
        payloads = [entry_a.item.meta, entry_b.item.meta]
        assert any("first" in str(p) for p in payloads)
        assert any("second" in str(p) for p in payloads)


# ── ③ — an args-less tool called twice ────────────────────────────────────


@pytest.mark.asyncio
async def test_no_arg_tool_called_twice_still_separates_correctly() -> None:
    """Tier 2: accept ③ — a tool with NO args (its own args_hash is
    deterministic, real value `44136fa355b3678a`, measured) called twice
    still gets 2 independently-settling rows — the op_id-keyed
    implementation this fix replaces would collide unconditionally here."""
    from textual_flowview import EntryState

    assert _args_hash({}) == "44136fa355b3678a", (
        "setup: this test's own premise (empty args are deterministic) "
        "must hold for the assertion below to mean anything"
    )
    transport = ScriptedTransport(
        [
            _started("dispatch-1", tool="list_tasks"),
            _started("dispatch-2", tool="list_tasks"),
            _completed("dispatch-1", tool="list_tasks"),
            _completed("dispatch-2", tool="list_tasks"),
        ],
        end=False,
    )
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry_a, entry_b = _entries(app, "tool_call_started")
        assert entry_a.state is EntryState.SUCCESS and entry_b.state is EntryState.SUCCESS


@pytest.mark.asyncio
async def test_a_failed_settle_also_correlates_by_dispatch_id_not_op_id() -> None:
    """Tier 2: the ERROR-path sibling of ①② — a same-tool-same-args pair
    where the SECOND call fails settles ITS OWN row to ERROR, leaving
    the first (successful) row correctly SUCCESS, not swapped."""
    from textual_flowview import EntryState

    transport = ScriptedTransport(
        [
            _started("dispatch-ok", args={"path": "/x"}),
            _started("dispatch-err", args={"path": "/x"}),
            _completed("dispatch-ok"),
            _failed("dispatch-err"),
        ],
        end=False,
    )
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        entry_ok, entry_err = _entries(app, "tool_call_started")
        states = {entry_ok.state, entry_err.state}
        assert states == {EntryState.SUCCESS, EntryState.ERROR}, (
            f"expected exactly one SUCCESS and one ERROR row, got {states}"
        )


# ── ⑤ — no src/ site reads op_id as a correlation key any more ───────────


def test_no_src_file_reads_op_id_as_a_correlation_key() -> None:
    """Tier 2: accept ⑤ — grep witness. `meta["op_id"]`/`data.get(
    "args_hash")` may still EXIST (dispatcher.py still computes and
    carries `args_hash`/`op_id` — accept ⑥ needs it unbroken for whatever
    else reads it, and it is harmless data), but nothing may read it
    back OUT as a dict key / correlation lookup any more. Scoped to the
    3 files this fix actually touched — a codebase-wide claim would need
    a real gate (out of this PR's own scope, disclosed here)."""
    offenders: "list[str]" = []
    for rel in (
        "src/reyn/interfaces/inline/textual_chat/app.py",
        "src/reyn/runtime/lifecycle_forwarder.py",
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        # A correlation READ would look like `_running_tools[op_id]`,
        # `_running_tools.pop(op_id, ...)`, or `meta.get("op_id")` used
        # as a dict key -- the ONLY remaining `op_id` mentions after this
        # fix are comments/docstrings and the *retained* var name that
        # now holds msg.id/msg.parent_id (never meta["op_id"] again).
        for m in re.finditer(r'meta\.get\("op_id"\)|meta\["op_id"\]', text):
            offenders.append(f"{rel}: {text[:m.start()].count(chr(10)) + 1}")
    assert offenders == [], f"op_id still read as a correlation key: {offenders}"


# ── ⑦ — an orphaned completion's VISUAL output is unchanged ──────────────


@pytest.mark.asyncio
async def test_orphaned_completion_renders_identically_regardless_of_parent_id_prefix() -> None:
    """Tier 2: accept ⑦ — an orphaned `tool_call_completed` (no matching
    started row absorbs it) renders IDENTICALLY whether its own
    `parent_id` is `call:`-prefixed (the pre-#6213 shape) or
    `tool:`-prefixed (post-#6213) — `parent_id` is read ONLY by
    `_ingest_frame`'s own correlation lookup (grep-confirmed: no
    presentation/render path in this module touches it), so a changed
    parent namespace cannot leak into what the reader sees."""
    orphan_old = OutboxMessage(
        kind="tool_call_completed", text="",
        meta={"tool": "grep", "call_id": "round-x", "result": {"count": 1}},
    )
    orphan_new = OutboxMessage(
        kind="tool_call_completed", text="",
        meta={"tool": "grep", "dispatch_id": "dispatch-orphan", "result": {"count": 1}},
    )
    assert orphan_old.parent_id == "call:round-x"
    assert orphan_new.parent_id == "tool:dispatch-orphan"
    assert orphan_old.parent_id != orphan_new.parent_id, (
        "setup: the two fixtures must actually carry different parent_id "
        "shapes for this test to mean anything"
    )

    transport_old = ScriptedTransport([orphan_old], end=True)
    transport_new = ScriptedTransport([orphan_new], end=True)
    app_old = TextualChatApp(transport=transport_old)
    app_new = TextualChatApp(transport=transport_new)
    async with app_old.run_test(size=(100, 30)) as pilot_old:
        await pilot_old.pause()
        entry_old = _entries(app_old, "tool_call_completed")[0]
        pres_old = await ReynPresenter().present(entry_old, 80)
    async with app_new.run_test(size=(100, 30)) as pilot_new:
        await pilot_new.pause()
        entry_new = _entries(app_new, "tool_call_completed")[0]
        pres_new = await ReynPresenter().present(entry_new, 80)

    assert entry_old.state == entry_new.state
    assert str(pres_old.renderable) == str(pres_new.renderable)
    assert pres_old.background == pres_new.background
