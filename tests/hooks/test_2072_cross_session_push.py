"""Tier 2: #2072 — cross-session hook push routing.

A push's ``session`` field was parsed + carried on the ResolvedPush but IGNORED: the
dispatcher always pushed to the CURRENT session (a documented no-op). #2072 wires it — a
``session`` naming a DIFFERENT session routes the push to THAT session's inbox (the canonical
wake-triple via the injected cross-session seam); ``null`` / empty / self stays LOCAL.

Real recording seams (no mocks), mirroring ``test_hook_dispatcher_1800_5b``.
"""
from __future__ import annotations

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock


class _Recorder:
    """A real recording async callable (not a mock) — captures (args, kwargs) per call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


def _dispatcher(hooks, *, current_session_id="me"):
    seams = {"put_inbox": _Recorder(), "stage": _Recorder(), "cross": _Recorder()}
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=seams["put_inbox"],
        stage_next_turn_context=seams["stage"],
        cross_session_put=seams["cross"],
        current_session_id=current_session_id,
    )
    return disp, seams


@pytest.mark.asyncio
async def test_cross_session_push_routes_to_target_not_current():
    """Tier 2: a push naming a DIFFERENT session delivers via the cross-session seam to that
    session, carrying the target sid + the wake flag — and does NOT land in the current
    session's inbox. RED if ``resolved.session`` is ignored (the pre-#2072 no-op: the push
    falls into the current session)."""
    hook = HookDef(on="turn_end",
                   template_push=PushBlock(message="hi peer", wake=True, session="peer-sid"))
    disp, seams = _dispatcher([hook], current_session_id="me")

    await disp.dispatch("turn_end", {})

    # exactly-one via tuple-unpack (the dispatcher idiom) — RED if zero or many
    (args, kwargs), = seams["cross"].calls
    assert args[0] == "peer-sid", "must target the named session"
    payload = args[2]
    assert payload["text"] == "hi peer" and payload["wake"] is True
    assert kwargs.get("wake") is True, "the wake flag must reach the cross-session put"
    assert seams["put_inbox"].calls == [], "must NOT land in the current session's inbox"
    assert seams["stage"].calls == []


@pytest.mark.asyncio
async def test_no_session_stays_local():
    """Tier 2: a push with no ``session`` stays LOCAL (the current session's inbox). RED if a
    null-session push wrongly routes through the cross-session seam."""
    hook = HookDef(on="turn_end", template_push=PushBlock(message="self note", wake=True))
    disp, seams = _dispatcher([hook], current_session_id="me")

    await disp.dispatch("turn_end", {})

    (args, _k), = seams["put_inbox"].calls  # exactly one local push
    assert args[1]["text"] == "self note"
    assert seams["cross"].calls == [], "a null-session push must NOT go cross-session"


@pytest.mark.asyncio
async def test_self_session_stays_local():
    """Tier 2: a push naming the CURRENT session stays local — no needless cross-session hop
    (the current session is reached directly). RED if a self-named push takes the
    cross-session path."""
    hook = HookDef(on="turn_end",
                   template_push=PushBlock(message="me note", wake=True, session="me"))
    disp, seams = _dispatcher([hook], current_session_id="me")

    await disp.dispatch("turn_end", {})

    (args, _k), = seams["put_inbox"].calls  # exactly one local push, no cross-session hop
    assert args[1]["text"] == "me note"
    assert seams["cross"].calls == []
