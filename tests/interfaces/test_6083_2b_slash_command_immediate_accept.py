"""Tier 2: #6083 ⑵-b — the AG-UI ``slash_command`` arm answers ``accepted``
the instant the command is HANDED OFF to the session's own background-task
funnel, not once it has finished running.

★ Why this closes what stage 1 (#6094) could not. Stage 1 fixed the false
CLAIM a slow session-locus command produced (see
``test_6083_slash_dispatch_control_outcome.py``), but the owner's own
``/compact`` case did not just read wrong — it took longer than the client's
control read timeout, full stop. This stage removes the wait itself: the
endpoint schedules ``execute_slash_command`` on ``Session._background_tasks``
(#4759's existing task funnel) and returns immediately. Real completion/
failure is not a new signal — ``Session._slash_context()`` already wires
``SessionBoundTransport(display_sink=self._put_outbox_nowait)``, so a
background-run command's own success/error line rides the SAME outbox → SSE
broadcast every other reply does.

Two legs, both real ``AgentRegistry`` + ``Session`` (no mock/stub of either —
testing.md's "never fake a collaborator when a real instance is cheaply
constructible"):

- the positive: the response returns ``accepted: true`` while a
  externally-gated handler is STILL BLOCKED — proof the server did not wait,
  driven by an ``asyncio.Event`` the test itself controls (never a sleep;
  testing.md's no-duration rule) — then releasing the gate and observing the
  command's own completion arrive later, on the display stream.
- the negative (condition ⑴ of lead-coder's design review): if
  ``TrackedTaskSet.spawn`` itself raises before the task can even start, the
  response must NOT still say ``accepted: true`` — the one shape that must
  never happen is "told it ran, nothing runs". ``spawn`` is monkeypatched on
  the REAL, already-constructed ``TrackedTaskSet`` instance (one method on a
  real collaborator, not a mock object) to make that path reachable at all;
  there is no way to make the real funnel fail on demand otherwise.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash import REGISTRY, SlashCommand
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _one_session_registry(tmp_path, monkeypatch) -> "tuple[AgentRegistry, dict]":
    """Same shape as ``test_3595_s5_session_does_not_interpret_text.py``'s own
    helper of the same name — kept local rather than imported across test
    modules, matching this repo's existing convention of no cross-test-file
    helper imports."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    reg.create("operator")
    reg.get_or_load("operator")
    return reg, holder


def _app_and_client(reg, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from reyn.interfaces.transport.agui import endpoint as endpoint_mod
    from reyn.interfaces.transport.agui.endpoint import router
    from reyn.interfaces.web.auth import AuthContext

    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token="s3cret", require_token=True)
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: reg)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_accept_returns_while_the_handler_is_still_running(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the response lands before the handler's own gate is released."""
    reg, _holder = _one_session_registry(tmp_path, monkeypatch)
    session = reg._peek_session("operator")
    assert session is not None
    gate = asyncio.Event()
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def _blocking_handler(ctx, args: str) -> None:
        entered.set()
        await gate.wait()
        finished.set()

    monkeypatch.setitem(
        REGISTRY._commands, "__f6083b_block__",
        SlashCommand(
            name="__f6083b_block__", summary="test",
            handler=_blocking_handler, locus="session",
        ),
    )

    sub = session.outbox_hub.subscribe()
    async with _app_and_client(reg, monkeypatch) as client:
        resp = await client.post(
            "/agui/chat/operator?token=s3cret",
            json={"type": "slash_command", "name": "__f6083b_block__", "args": ""},
        )
        assert resp.status_code == 200 and resp.json().get("accepted") is True, (
            f"expected an immediate accept: {resp.status_code} {resp.text}"
        )
        # Unbounded wait on the real signal the handler itself raises on
        # entry — never a sleep (testing.md). If the endpoint had instead
        # AWAITED the handler to completion (the pre-⑵-b shape), this would
        # already be true by the time ``resp`` came back, so this assertion
        # on its own would not distinguish old from new behavior — the
        # point being proven is the ORDER: the response above already
        # returned, and only NOW do we confirm the handler had a chance to
        # run at all.
        await entered.wait()
        assert not gate.is_set(), (
            "sanity: the handler must still be gated when we check this"
        )
        gate.set()
        # Let the background task actually finish before the client (and
        # this session) tear down — waited on ``finished`` (a signal this
        # handler itself raises on its own exit), never on
        # ``session._background_tasks.pending()``: that set also holds the
        # SESSION's own session-lifetime producers (the outbox drain loop,
        # the mcp tools probe), which never empty on their own, so polling
        # it to become ``[]`` here would hang on tasks this test never
        # spawned and has no reason to wait for.
        await finished.wait()
        sub.close()


@pytest.mark.asyncio
async def test_a_scheduling_failure_does_not_still_answer_accepted(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: condition ⑴ (lead-coder) — if ``spawn()`` itself fails before
    the task can start, the response must say ``accepted: false`` — not the
    one shape that must never happen, "told it ran, nothing runs"."""
    reg, _holder = _one_session_registry(tmp_path, monkeypatch)
    session = reg._peek_session("operator")
    assert session is not None

    ran = asyncio.Event()

    async def _handler(ctx, args: str) -> None:
        ran.set()  # pragma: no cover - must never be reached

    monkeypatch.setitem(
        REGISTRY._commands, "__f6083b_fail__",
        SlashCommand(name="__f6083b_fail__", summary="test", handler=_handler, locus="session"),
    )

    def _raise(*_a, **_kw):
        raise RuntimeError("simulated scheduling failure")

    monkeypatch.setattr(session._background_tasks, "spawn", _raise)

    async with _app_and_client(reg, monkeypatch) as client:
        resp = await client.post(
            "/agui/chat/operator?token=s3cret",
            json={"type": "slash_command", "name": "__f6083b_fail__", "args": ""},
        )
        assert resp.status_code == 200 and resp.json().get("accepted") is False, (
            f"a spawn() failure must not still answer accepted:true: "
            f"{resp.status_code} {resp.text}"
        )
    assert not ran.is_set(), (
        "the handler ran despite spawn() raising -- the fixture itself is "
        "not exercising the failure path this test is named for"
    )
