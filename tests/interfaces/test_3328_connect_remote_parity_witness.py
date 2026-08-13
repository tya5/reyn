"""Tier 2: #3328 — an explicit witness for ``--connect`` remote parity.

**Reachability finding (read this before extending the file).** #3328 asked,
first, whether ``--connect`` can be witnessed at all without becoming a
skip-in-every-environment gate (the sandbox-Landlock failure mode this repo has
already been bitten by — see ``verification-hazards.md``). Landlock needs a real
Linux kernel feature this suite's CI runners mostly lack, hence that gate's
standalone-script-with-FATAL-preconditions shape. ``--connect``'s only
precondition is a loopback TCP bind + the ``web`` extra (fastapi/uvicorn) —
BOTH already present in the standard ``pytest`` CI job (``test.yml`` installs
``web`` unconditionally, and ``tests/mcp/test_mcp_client.py``'s
``_HttpEchoServer`` already runs a real bound ``127.0.0.1`` server, as a
background asyncio task, inside that exact job today). So unlike Landlock,
**this witness runs as an ordinary Tier 2 pytest test in the standard suite —
no dedicated workflow, no skip path, no FATAL-precondition script.** It binds a
REAL socket (``uvicorn.Server`` serving the REAL ``agui`` router) and drives it
with the REAL ``reyn chat --connect`` client entry point
(:func:`reyn.interfaces.repl.remote_client.run_remote_repl`) — the one thing no
existing test exercised (every prior AG-UI parity test, incl. #3310 N3's
``test_3310_n3_remote_switch_parity.py``, drives the emitter/transport pair
directly off a hand-fed SSE text generator — never over a real socket, and
never through ``run_remote_repl`` itself).

**Parity properties pinned here (two — not "parity" in general):**

1. **Connect-time backlog reconstruction** (``test_connect_over_real_socket_...``):
   a session's existing history — user / assistant / a tool call+result pair —
   reconstructs, over the REAL wire via the REAL ``run_remote_repl``, into the
   IDENTICAL sequence a local hydrate (``project_restored_frames`` read straight
   off ``session.history`` — the same oracle #3310 N3 uses) would show. This is
   the one property #3310 N3 already covers logically but never over a live
   socket + the real CLI entry point — this closes THAT specific gap, not a new
   claim about content shaping.
2. **Live intervention delivery + answer round-trip** (``test_connect_intervention_...``):
   an ``ask_user`` intervention dispatched on the session WHILE a real remote
   connection is attached is delivered over the wire (the client's
   ``AgUiTransport`` tracks it as ``pending_intervention_head()``), and an
   over-the-wire answer (a real ``TOOL_CALL_RESULT`` POST, driven by the
   production ``AgUiTransport.answer_intervention_text``) resolves the SAME
   ``InterventionAnswer`` shape (raw, unfenced ``text``) a LOCAL direct answer
   (``session.answer_oldest_intervention_text``) produces for an equivalent
   local intervention — both go through ``external_source=False`` (operator,
   unfenced — the P0 keystone), verified on both paths in the same test.

**Explicitly NOT covered here** (named, not silently absent, per the "either
cover what you name or name only what you cover" instruction): the tool/
capability envelope offered to the LLM, audit-event content parity beyond the
two properties above, and cancellation. Each is a legitimate follow-up scope,
not folded into this witness's claim.

Real ``AgentRegistry`` + real ``Session`` (``tests._support.agent_session
.make_session``), the real ``agui`` FastAPI router, a real bound
``uvicorn.Server``, real ``httpx`` over the loopback socket, and the real
``run_remote_repl``/``AgUiTransport``/``run_chat_client`` client stack — no
mocks anywhere in the conveyance path.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys

import pytest
import uvicorn
from fastapi import FastAPI

from reyn.interfaces.inline.textual_chat.restore import project_restored_frames
from reyn.interfaces.repl.remote_client import run_remote_repl
from reyn.interfaces.repl.renderer import ChatRenderer
from reyn.interfaces.transport.agui import endpoint as endpoint_mod
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.web.auth import AuthContext
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.user_intervention import UserIntervention
from tests._async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper
from tests._support.agent_session import make_session

# ── shared harness: a REAL bound server + REAL registry ─────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _registry(tmp_path) -> AgentRegistry:
    shared = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


class _RealServer:
    """A REAL ``uvicorn.Server`` bound to a REAL loopback socket, serving the
    REAL ``agui`` router — the server half of ``--connect``. Mirrors
    ``tests/mcp/test_mcp_client.py``'s ``_HttpEchoServer`` (real bound socket, real
    background asyncio task, no subprocess needed — that pattern already runs
    green in the standard CI ``pytest`` job today)."""

    #: Every real TCP bind (loopback included) always carries a token in
    #: production — `reyn web`'s own `_apply_auth_startup` generates one for a
    #: tokenless loopback bind before `uvicorn.run`, and `AuthContext.
    #: authenticate` requires a non-empty presented token for BOTH the
    #: loopback and network tiers (`verify_token` returns False whenever
    #: either side is empty — there is no tokenless-loopback bypass at the
    #: authenticate() layer itself). A real, fixed test token here matches
    #: that reality; it is not a relaxed/open posture.
    TOKEN = "test-connect-parity-token"  # noqa: S105 — a test fixture value, not a secret

    def __init__(self, registry: AgentRegistry) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        app = FastAPI()
        app.include_router(endpoint_mod.router)
        app.state.auth = AuthContext(token=self.TOKEN, require_token=True)
        self._registry = registry
        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off",
        )
        self._server = uvicorn.Server(self._config)
        self._task: "asyncio.Task | None" = None

    async def __aenter__(self) -> "_RealServer":
        # The endpoint module resolves the registry via a plain top-level
        # `get_registry()` call (not a FastAPI `Depends`, so
        # `app.dependency_overrides` cannot reach it) — redirect the seam
        # directly, the same "patch the external/boundary function" pattern
        # `test_web_ws_max_size_config_1934.py` uses for `uvicorn.run`.
        self._orig_get_registry = endpoint_mod.get_registry
        endpoint_mod.get_registry = lambda: self._registry
        self._task = asyncio.create_task(self._server.serve())
        await wait_until(lambda: self._server.started)
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=10.0)
        endpoint_mod.get_registry = self._orig_get_registry


class _RecordingRenderer(ChatRenderer):
    """A real (non-mock) minimal ``ChatRenderer`` implementation — the
    interface's documented contract is that every method is independently
    overridable with no-op defaults (``renderer.py``'s ``ChatRenderer``
    docstring), so recording instead of no-op'ing is ordinary usage of the
    interface, not a faked collaborator."""

    def __init__(self) -> None:
        self.messages: "list" = []

    def message(self, msg) -> None:
        self.messages.append(msg)


class _PipeStdin:
    """A real OS pipe, one end installed as ``sys.stdin`` — the exact shape a
    genuine ``reyn chat --connect < script.txt`` invocation presents to
    ``stream_client.run_input_loop`` (``is_tty=False`` -> ``sys.stdin.readline``
    via executor, and prompt_toolkit's ``create_input()`` needs a real
    ``fileno()`` even off the non-interactive branch). The reader end IS
    ``sys.stdin`` (``os.fdopen`` — a genuine file object, real ``fileno``/
    ``isatty``/``readline``, not a hand-rolled stand-in); this test only owns
    the writer end, exactly like a shell redirect would."""

    def __init__(self) -> None:
        r, w = os.pipe()
        self.reader = os.fdopen(r, "r")
        self._writer = os.fdopen(w, "w")

    def send_line(self, text: str) -> None:
        self._writer.write(text + "\n")
        self._writer.flush()

    def close(self) -> None:
        self._writer.close()
        self.reader.close()


# ── Property 1: connect-time backlog reconstruction over the REAL wire ──────


@pytest.mark.asyncio
async def test_connect_over_real_socket_reconstructs_history_like_local_hydrate(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: seed a session's history (user / assistant / a tool call+result
    pair), connect to it over a REAL bound socket via the REAL
    ``run_remote_repl`` entry point, and assert the client renders EXACTLY the
    local oracle (``project_restored_frames(session.history)``) would — proven
    over the genuine ``--connect`` conveyance, not a hand-fed SSE generator.

    Strip-falsify (verified manually, not left in the tree): temporarily made
    ``endpoint.session_backlog_frames`` return ``[]`` unconditionally -> this
    test's tool/agent/user text assertions went RED (nothing rendered at all,
    only the connect banner) -> reverted, GREEN. Confirms the assertions
    actually exercise the backlog wire, not a vacuous pass.
    """
    monkeypatch.chdir(tmp_path)
    registry = _registry(tmp_path)
    session = await registry.attach("alpha")
    session.history.append(ChatMessage(role="user", content="what's the weather?"))
    session.history.append(
        ChatMessage(
            role="assistant", content="Let me check.",
            tool_calls=[{"id": "tc1", "type": "function",
                         "function": {"name": "weather", "arguments": "{}"}}],
        )
    )
    session.history.append(
        ChatMessage(role="tool", content="sunny, 21C", tool_call_id="tc1", name="weather")
    )
    session.history.append(ChatMessage(role="assistant", content="It's sunny, 21C."))

    local_oracle = project_restored_frames(list(session.history))
    assert local_oracle, "local oracle must reconstruct something to compare against"

    async with _RealServer(registry) as server:
        renderer = _RecordingRenderer()
        stdin = _PipeStdin()
        monkeypatch.setattr(sys, "stdin", stdin.reader)

        remote_task = asyncio.create_task(
            run_remote_repl(
                base_url=server.base_url, agent_name="alpha", token=server.TOKEN,
                renderer=renderer,
            )
        )
        # Wait for the connect-time MESSAGES_SNAPSHOT backlog to actually
        # render before quitting — sending "/quit" immediately races the
        # input loop's readline() against the SSE handshake + backlog
        # delivery (the input loop can win, ending the client having
        # rendered nothing at all, a false pass this guards against).
        await wait_until(lambda: len(renderer.messages) >= len(local_oracle))
        stdin.send_line("/quit")
        await asyncio.wait_for(remote_task, timeout=10.0)
        stdin.close()

    rendered_kinds_texts = [(m.kind, m.text) for m in renderer.messages]
    oracle_kinds_texts = [(m.kind, m.text) for m in local_oracle]
    assert rendered_kinds_texts == oracle_kinds_texts, (
        f"remote-rendered backlog {rendered_kinds_texts!r} != "
        f"local hydrate oracle {oracle_kinds_texts!r}"
    )
    # Sanity the oracle itself is non-trivial (would catch a degenerate
    # "both empty" false pass): the tool call's coalesced meta must be present.
    tool_frame = next(m for m in local_oracle if m.kind == "tool_call_started")
    assert tool_frame.meta.get("tool") == "weather"


# ── Property 2: live intervention delivery + answer round-trip ──────────────


@pytest.mark.asyncio
async def test_connect_intervention_round_trip_matches_local_unfenced_answer(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: an ``ask_user`` intervention dispatched WHILE a real remote
    connection is attached is delivered over the wire and answerable through
    the production remote answer path (``AgUiTransport.answer_intervention_text``
    -> a real ``TOOL_CALL_RESULT`` POST) — resolving to the SAME unfenced
    ``InterventionAnswer`` shape a LOCAL direct answer produces for an
    equivalent intervention on the same session.

    Drives :func:`run_output_loop` directly (the real frame-drain loop) rather
    than the full :func:`run_chat_client` — the interactive PromptSession/stdin
    input driver is orthogonal to the property under test (wire delivery +
    answer parity) and is already covered, unrelated to interventions, by
    ``test_agui_remote_inline_p3.py``'s ``test_run_chat_client_drives_remote_
    transport_to_end``. The answer itself still rides the exact production
    ``AgUiTransport`` method a real client's input loop would call.

    Strip-falsify (verified manually, not left in the tree): temporarily
    dropped ``session.register_intervention_listener`` from the endpoint's
    connect handler -> the dispatch task never resolved within the timeout
    (no listener -> nothing answers it) -> this test's ``asyncio.wait_for``
    raised -> RED -> reverted, GREEN.
    """
    monkeypatch.chdir(tmp_path)
    registry = _registry(tmp_path)
    session = await registry.attach("alpha")

    async with _RealServer(registry) as server:
        import httpx

        from reyn._network import build_async_http_client
        from reyn.interfaces.repl.stream_client import run_output_loop

        events_url = f"{server.base_url}/agui/chat/alpha/events"
        submit_url = f"{server.base_url}/agui/chat/alpha"
        # A fixed connection_id shared by the GET (events) and every POST — the
        # same shape `remote_client.py` uses (one `connection_id` minted before
        # any submit, stamped on every request) so the answer POST is
        # recognised as coming from the SAME connection the SSE stream (and
        # therefore the active-driver / surface registration) is bound to.
        params = {"token": server.TOKEN, "connection_id": "test-conn-1"}

        async def _post(payload: dict) -> "dict | None":
            resp = await client.post(submit_url, params=params, json=payload)
            if resp.status_code >= 300:
                return None
            try:
                return resp.json()
            except Exception:  # noqa: BLE001 — an empty 2xx body is still an accept
                return {"status": "ok"}

        async with build_async_http_client(
            timeout=httpx.Timeout(None, connect=10.0), egress="remote_repl_test",
        ) as client:
            async with client.stream("GET", events_url, params=params) as resp:
                assert resp.status_code < 400

                async def _sse_lines():
                    async for line in resp.aiter_lines():
                        yield line

                transport = AgUiTransport(_sse_lines(), _post)
                renderer = _RecordingRenderer()
                output_task = asyncio.create_task(run_output_loop(transport, renderer))

                # Wait for connect to register the operator listener before
                # dispatching (mirrors production: a real client always
                # connects, THEN a turn may raise an intervention).
                await wait_until(
                    lambda: session._interventions.has_active_listener(),
                )

                remote_iv = UserIntervention(kind="ask_user", prompt="Proceed?", run_id="r-remote")
                remote_task = asyncio.ensure_future(
                    session._intervention_handler.dispatch(remote_iv)
                )
                await wait_until(
                    lambda: transport.pending_intervention_head() is not None,
                )
                assert renderer.messages, "the intervention prompt must render before it is answered"

                # The production remote answer path: the real AgUiTransport
                # method a real client's input loop calls, over the real wire.
                answered = await transport.answer_intervention_text("yes-over-the-wire")
                assert answered is True
                remote_answer = await asyncio.wait_for(remote_task, timeout=5.0)

                output_task.cancel()
                await asyncio.gather(output_task, return_exceptions=True)

    # Local oracle: an equivalent intervention answered DIRECTLY on the same
    # session (the local REPL's own answer path), same fence posture. The
    # remote connection above already detached (the `async with _RealServer`
    # block exited), so a local listener is registered fresh here — a real
    # local REPL always has one (`run_repl` registers on attach); without it
    # `InterventionRegistry.dispatch`'s listener-enforce would auto-refuse
    # before ever reaching "pending", which is not the property being compared.
    session.register_intervention_listener("test-local")
    local_iv = UserIntervention(kind="ask_user", prompt="Proceed?", run_id="r-local")
    local_task = asyncio.ensure_future(session._intervention_handler.dispatch(local_iv))
    await wait_until(lambda: bool(session._interventions.list_active()))
    local_answered = await session.answer_oldest_intervention_text("yes-locally")
    assert local_answered is True
    local_answer = await asyncio.wait_for(local_task, timeout=5.0)

    # Parity: both are unfenced (operator, external_source=False), free-text
    # answers with no refusal — the RAW text survives untouched on both paths.
    assert remote_answer.text == "yes-over-the-wire"
    assert local_answer.text == "yes-locally"
    assert remote_answer.refused is False
    assert local_answer.refused is False
    assert remote_answer.choice_id is None
    assert local_answer.choice_id is None
