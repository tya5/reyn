"""Tier 2: the client may render before `registry.attach()` completes (#3671 P2).

Before this PR, `chat.py`'s `_main_chat` awaited `registry.restore_all()` +
`registry.attach(name)` to completion BEFORE calling `run_repl` at all — and
`run_repl` itself raised `RuntimeError` if nothing was attached yet. For a
project with many in-flight agents, `restore_all()` synchronously builds a
full `Session` for every one of them (see `AgentRegistry.restore_all`'s own
step 5) before the user ever sees the shell — a D4 (never-shown-this-run)
cost sitting on the D0 (must-happen-before-first-paint) critical path (#3671's
startup-latency investigation).

This PR moves `restore_all()` + `attach()` off the render path: `run_repl` no
longer requires a prior attach (`InProcessTransport`'s accessors, and
`AgentRegistry._wire_focus_listeners`, already tolerated an unattached
registry — see the two tests below, which witness that tolerance directly on
the REAL production seams, no mocks), and `chat.py` now starts `run_repl`
immediately while a background task performs the restore/attach.

Three things are proven here, matching the acceptance conditions from the
#3671 P2 dispatch (the third added in #3675's review round):

1. `has_session()` (the transport's own public read of "is anything
   attached") is observably `False` before `attach()` and `True` after — the
   VALUE witness the dispatch asked for, not just "it didn't raise".
2. `run_repl` itself no longer requires an attach to have already happened —
   proven by driving it with NOTHING ever attached and confirming it still
   completes without raising `RuntimeError`, with the client seeing
   `agent_name` derived from the caller's intended target (not from an
   attached `Session`, which doesn't exist in this scenario).
3. A background `attach()` failure genuinely does NOT crash the caller — the
   RESULT tested (per lead-coder's #3675 review: "test the result, not the
   presence of try/except"), driven through the real, now-module-level
   `chat._background_attach` with a `session_factory` that raises.

Real `AgentRegistry` + real `Session` throughout (`tests/_support/agent_session
.make_session`, the same helper `test_registry_focus_listener_rewire.py` and
`test_teardown_completeness_2783.py` use) — the only substitution is
`repl.run_chat_client`, the shared UI input/output loop (owned by
`client_driver.py`, explicitly OUT of #3671 P2's scope — the dispatch named
UI consumption of `has_session()` as P3, not this PR) swapped for a small
async stand-in so this test observes the COMPOSITION change (`run_repl` no
longer gates on attach) without needing to drive a real terminal I/O loop.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.cli.commands.chat import _background_attach
from reyn.interfaces.repl import repl as repl_mod
from reyn.interfaces.repl.renderer import ChatRenderer
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from tests._support.agent_session import make_session


def _registry(tmp_path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


@pytest.mark.asyncio
async def test_transport_has_session_false_before_attach_true_after(tmp_path):
    """Tier 2: #3671 P2's own load-bearing precondition, witnessed directly
    on the real `InProcessTransport` + `AgentRegistry` — a caller can build
    and start the transport, observe `has_session() is False`, THEN attach,
    and observe `has_session() is True` immediately after — no ordering
    requirement between transport construction/start and attach."""
    reg = _registry(tmp_path)
    transport = InProcessTransport(reg, intervention_channel=DEFAULT_CHAT_CHANNEL_ID)
    transport.start()
    try:
        assert transport.has_session() is False, (
            "a fresh, never-attached registry must read as 'no session' — "
            "this is the exact value the client renders behind"
        )
        await reg.attach("alpha")
        assert transport.has_session() is True, (
            "has_session() must flip True the moment attach() completes"
        )
    finally:
        transport.close()
        await reg.shutdown()


@pytest.mark.asyncio
async def test_run_repl_completes_with_nothing_ever_attached(tmp_path, monkeypatch):
    """Tier 2: #3671 P2 — `run_repl` no longer raises `RuntimeError` when
    called before (or, as here, entirely without) an attach. The stub
    `run_chat_client` observes `registry.attached_session() is None` at the
    moment the client would start rendering — proving the client is allowed
    to come up while `has_session()` is still False — and receives
    `agent_name="alpha"` from the CALLER's intended target, not from a
    (nonexistent) attached Session."""
    reg = _registry(tmp_path)
    seen: dict = {}

    async def _fake_run_chat_client(*, transport, renderer, read_model, agent_name, is_tty, config=None, own_connection_id=None):
        seen["agent_name"] = agent_name
        seen["attached_at_render_start"] = reg.attached_session()
        seen["has_session_at_render_start"] = transport.has_session()

    monkeypatch.setattr(repl_mod, "run_chat_client", _fake_run_chat_client)

    try:
        await repl_mod.run_repl(reg, ChatRenderer(), name="alpha", config=None)
    finally:
        await reg.shutdown()

    assert seen["agent_name"] == "alpha"
    assert seen["attached_at_render_start"] is None, (
        "the client rendered while nothing was attached — the whole point of #3671 P2"
    )
    assert seen["has_session_at_render_start"] is False


@pytest.mark.asyncio
async def test_run_repl_survives_background_attach_racing_the_render_start(tmp_path, monkeypatch):
    """Tier 2: acceptance condition ① end-to-end — a background attach
    running CONCURRENTLY with `run_repl` (mirroring `chat.py`'s
    `_background_attach` task) does not corrupt or block the client: the
    stub observes `has_session()` still False at its own start (attach
    hasn't landed yet), and the registry ends up attached once the
    background task finishes, all without `run_repl` raising."""
    reg = _registry(tmp_path)
    seen: dict = {}
    release_attach = asyncio.Event()

    async def _fake_run_chat_client(*, transport, renderer, read_model, agent_name, is_tty, config=None, own_connection_id=None):
        seen["has_session_at_render_start"] = transport.has_session()
        release_attach.set()
        # give the background attach task a chance to actually run
        await asyncio.sleep(0)

    async def _simulated_background_attach() -> None:
        await release_attach.wait()
        await reg.attach("alpha")

    monkeypatch.setattr(repl_mod, "run_chat_client", _fake_run_chat_client)

    attach_task = asyncio.create_task(_simulated_background_attach())
    try:
        await repl_mod.run_repl(reg, ChatRenderer(), name="alpha", config=None)
    finally:
        await attach_task
        await reg.shutdown()

    assert seen["has_session_at_render_start"] is False
    assert reg.attached_session() is not None, "background attach must still land"


@pytest.mark.asyncio
async def test_background_attach_failure_leaves_client_alive_with_no_session(tmp_path):
    """Tier 2: acceptance condition ② — the REAL `chat.py` `_background_attach`
    (module-level since #3675's review, so it is directly callable here — no
    private-closure workaround needed), driven with a `session_factory` that
    raises on attach (a real production failure mode: e.g. a corrupt on-disk
    profile, a bad recovery build). lead-coder's review of #3675 specifically
    asked for the RESULT to be tested, not the presence of a try/except: does
    `_background_attach` actually complete without raising, leaving the
    registry unattached, rather than propagating and killing the caller (which
    in production is the task `_main_chat` awaits — an uncaught exception
    there would tear down the whole client, the opposite of 'client
    survives')."""

    def _failing_factory(profile: AgentProfile) -> Session:
        raise RuntimeError("simulated attach failure (#3675 P2 review)")

    reg = AgentRegistry(project_root=tmp_path, session_factory=_failing_factory)
    reg.create("alpha")

    await _background_attach(reg, "alpha", skip_restore=True)  # must not raise

    assert reg.attached_session() is None, (
        "attach genuinely failed — the registry must stay unattached, not "
        "half-attached or attached-to-a-broken-session"
    )
