"""Tier 2: no non-operator inbox producer can execute a registered slash command.

#3595 step 1 closed the pipeline ``agent`` step's prompt (``AGENT_STEP_INBOX_KIND``);
``tests/test_3561_spawn_session_seam_reachability.py`` holds that leg. Step 1b closes
the three producers that were left claiming ``kind="user"`` with NON-EMPTY text:

  * ``reyn.gateway.api.push_to_agent`` — the stable public webhook API every chat
    gateway plugin routes through (Slack / LINE samples ship in-tree). Its text is
    written by whoever can post to the webhook.
  * ``reyn.mcp.server.send_to_agent_impl`` — reached by the MCP ``send_to_agent``
    tool and by the A2A JSON-RPC router. Its text is written by a peer process,
    frequently another LLM.
  * the ``reyn web`` cron runner's inbox push — an unattended timer firing an
    operator-authored job message into a session with no client attached.

``"user"`` is the claim ``Session._handle_user_message`` acts on by handing a
``/``-prefixed line to ``_maybe_handle_slash`` BEFORE any router turn, so under that
kind a Slack message reading ``/reset`` executed the command. Each producer now
rides its own member of the union ``_run_turn_body`` already dispatches on
(``runtime.transport.EXTERNAL_MESSAGE_INBOX_KIND`` for the first two,
``runtime.cron.routing.CRON_INBOX_KIND`` for the third), which routes straight to
the shared turn body: the dispatch is not skipped by a flag, it is not on those
paths at all.

**What each leg asserts, and why both halves are needed.** The observable is a real
side effect outside the session — ``/session new`` opens a conversation session under
the attached agent, so ``AgentRegistry.session_ids`` moves if and only if the command
ran. Each leg asserts (a) that it did NOT move, and (b) that the scripted LLM WAS
consulted. (b) is load-bearing: without it, "no session was born" has a second
explanation — the turn never ran at all — which is exactly what an absence-only leg
cannot tell apart. Together they say the line arrived, and arrived as text.

``test_an_operator_submitted_slash_command_still_spawns_a_session`` is the control
for all three, on the same harness: the same command through
``Session.submit_user_text`` (the one public entry every client's composer ends at)
still executes. Without it, deleting slash dispatch outright — or a harness that
silently runs no turns — would pass every absence leg above.

**The product decision this file records.** Slash is deliberately NOT exposed to
these transports (owner ruling, 2026-08-01: 「現時点では slash に公開不要」 — "no
need to expose slash at present"). A reader finding this behaviour later is looking
at a decision, not at a feature that was dropped by accident. If it is ever revisited,
the ratified route is a shared client-side slash layer — never ``startswith("/")``
returning to a transport.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.gateway.api import push_to_agent
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.mcp.server import send_to_agent_impl
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import PresentationWiring
from tests._support.agent_session import make_session
from tests._support.permissions import make_resolver

#: A real, registered slash command whose handler calls
#: ``AgentRegistry.spawn_session`` (``interfaces/slash/session.py::session_cmd``) —
#: the one command whose side effect is observable from OUTSIDE the session that
#: runs it, which is what lets every leg here assert on a real side effect rather
#: than on the inbox kind string. The invariant is not about ``/session new``: it is
#: that a non-operator kind reaches NO registered command, because the branch that
#: dispatches them is never entered.
_SLASH_LINE = "/session new"

#: How long a leg waits for a producer that hands its turn to a booted run-loop
#: (webhook / cron) rather than pumping it inline. The wait ends as soon as EITHER
#: outcome is visible, so a regression is fast, not a timeout.
_SETTLE_TIMEOUT_S = 10.0


class _ScriptedReply:
    """A real ``_llm_caller``-shaped callable answering with one fixed plain-text
    turn — the Tier-2c LLM stand-in this arc's sibling files already use (see
    ``tests/test_3561_spawn_session_seam_reachability.py``), NOT a ``MagicMock``:
    a signature drift in the ``call_llm_tools`` contract raises ``TypeError`` here
    exactly as it would in production.

    ``calls`` is load-bearing rather than diagnostic — every leg reads it to show
    the turn under test really consulted a model.
    """

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _registry(
    tmp_path: Path, scripted: _ScriptedReply, *, agents: "tuple[str, ...]" = ("worker", "operator"),
) -> AgentRegistry:
    """Real ``AgentRegistry`` + real ``Session`` factory — the harness shape
    ``tests/test_3561_spawn_session_seam_reachability.py`` uses, including the
    ``holder`` deferred-registry-ref so the factory can pass ``registry=``, and its
    real-litellm-name resolver so a spawned session's model-support pre-check has a
    name to resolve."""
    if not (tmp_path / "reyn.yaml").exists():
        (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}
    resolver = ModelResolver({"standard": "gemini/gemini-2.5-flash-lite"})
    perms = make_resolver(tmp_path, config={"file.write": "allow"})

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            permission_resolver=perms, workspace_base_dir=tmp_path,
            presentation_wiring=PresentationWiring(
                presentation_consumer=presentation_consumer,
                intervention_bridge=intervention_bridge,
            ),
            resolver=resolver,
        )
        s._loop_driver._loop_observer = (
            lambda loop: setattr(loop, "_llm_caller", scripted)
        )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    for name in agents:
        if not reg.exists(name):
            reg.create(name)
    return reg


async def _attach_operator(reg: AgentRegistry) -> "set[str]":
    """Attach an operator agent — the ordinary REPL state, and whose session set
    ``/session new`` acts on (``session_cmd`` reads ``registry.attached_name``).
    Returns the baseline session-id set to diff against."""
    reg.get_or_load("operator")
    await reg.attach_session("operator", "main")
    return set(reg.session_ids("operator"))


async def _settle(reg: AgentRegistry, scripted: _ScriptedReply, before: "set[str]") -> None:
    """Wait for a run-loop-driven producer's turn to become observable, then let
    every in-flight task finish. Exits as soon as EITHER outcome is visible (a
    session was born, or the model was consulted), so the failing direction is
    reported quickly instead of after the full timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _SETTLE_TIMEOUT_S
    while loop.time() < deadline:
        if scripted.calls > 0 or set(reg.session_ids("operator")) - before:
            break
        await asyncio.sleep(0.01)
    for sid in list(reg.session_ids("worker")):
        session = reg.get_session("worker", sid)
        if session is not None:
            await session.await_quiescent()


def _assert_no_command_ran(reg: AgentRegistry, before: "set[str]", producer: str) -> None:
    born = set(reg.session_ids("operator")) - before
    assert not born, (
        f"a slash command delivered by {producer} EXECUTED — a session was born under "
        f"the attached agent ({sorted(born)!r}). That producer is claiming an inbox "
        "kind whose text Session._handle_user_message hands to slash dispatch, which "
        "puts every registered slash command within reach of whoever writes that text "
        "(#3595 step 1b)"
    )


def _assert_the_turn_ran(scripted: _ScriptedReply, producer: str) -> None:
    assert scripted.calls > 0, (
        f"the turn {producer} delivered never consulted the LLM, so the absence above "
        "is not evidence the text was delivered as content — nothing may have run at "
        f"all (llm calls: {scripted.calls})"
    )


# ── the three producers step 1b closes ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_webhook_delivered_slash_command_is_read_not_executed(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a chat webhook's message cannot execute a registered slash command.

    Drives the real stable public API (``gateway.api.push_to_agent``) the in-tree
    Slack and LINE samples call, with the text an external party controls. Before
    #3595 step 1b this pushed ``kind="user"`` and the command ran: anyone able to
    post to the webhook could reach ``/reset``, ``/visibility``, ``/plugin``,
    ``/rewind`` and the rest.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _ScriptedReply("nothing to say")
    reg = _registry(tmp_path, scripted)
    before = await _attach_operator(reg)

    await push_to_agent(
        target_agent="worker",
        text=_SLASH_LINE,
        sender="slack:U456",
        registry=reg,
    )
    await _settle(reg, scripted, before)

    _assert_no_command_ran(reg, before, "gateway.api.push_to_agent")
    _assert_the_turn_ran(scripted, "gateway.api.push_to_agent")


@pytest.mark.asyncio
async def test_an_mcp_delivered_slash_command_is_read_not_executed(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: an MCP / A2A peer's message cannot execute a registered slash command.

    Drives the real ``send_to_agent_impl`` — the backing implementation of the MCP
    ``send_to_agent`` tool and of the A2A JSON-RPC router — taking its DEFAULT
    ``inbox_kind``, which is the point: a caller that says nothing about who wrote
    the text gets the kind that cannot execute a command.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _ScriptedReply("nothing to say")
    reg = _registry(tmp_path, scripted)
    before = await _attach_operator(reg)

    await send_to_agent_impl(reg, agent_name="worker", message=_SLASH_LINE, timeout=30.0)

    _assert_no_command_ran(reg, before, "mcp.server.send_to_agent_impl")
    _assert_the_turn_ran(scripted, "mcp.server.send_to_agent_impl")


@pytest.mark.asyncio
async def test_a_cron_fire_slash_message_is_read_not_executed(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a fired cron job's message cannot execute a registered slash command.

    Drives the production runner ``reyn web`` installs (``_make_cron_runner``), with
    a real ``CronJob``. The job's text is operator-authored, but authored as the
    AGENT'S PROMPT and delivered to an unattended session — so before #3595 step 1b
    a job message beginning with ``/`` ran an operator command with no client
    present to have asked for it or to see the result.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _ScriptedReply("nothing to say")
    reg = _registry(tmp_path, scripted)
    before = await _attach_operator(reg)

    from reyn.interfaces.web import deps as web_deps
    from reyn.interfaces.web.server import _make_cron_runner
    from reyn.runtime.cron import CronJob

    monkeypatch.setattr(web_deps, "_get_registry", lambda: reg)
    runner = _make_cron_runner()
    outcome = await runner(
        CronJob(name="nightly", schedule="0 9 * * *", to="worker", message=_SLASH_LINE),
    )
    assert outcome == "ok", (
        "the cron fire did not dispatch at all, so neither assertion below measures "
        f"anything about slash reachability (runner returned {outcome!r})"
    )
    await _settle(reg, scripted, before)

    _assert_no_command_ran(reg, before, "the reyn web cron runner")
    _assert_the_turn_ran(scripted, "the reyn web cron runner")


# ── the control ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_operator_submitted_slash_command_still_spawns_a_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the CONTROL for the three legs above — slash dispatch is closed to
    non-operator producers and not to everyone.

    Same harness, same command, same observable; the only difference is the door the
    text comes through — ``Session.submit_user_text``, the one public entry every
    client's composer ends at, CUI and TUI alike. Without this leg, a harness that
    ran no turns, or a change that deleted slash dispatch outright, would pass all
    three absences.

    ``scripted.calls`` staying at 0 is the other half: the operator's line
    short-circuited at ``_maybe_handle_slash`` before any router turn, which is what
    tells an executed command apart from a model that decided to spawn.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _ScriptedReply("nothing to say")
    reg = _registry(tmp_path, scripted)
    before = await _attach_operator(reg)

    operator = reg.get_or_load("operator")
    await operator.submit_user_text(_SLASH_LINE)
    await operator.run_one_iteration()
    await operator.await_quiescent()

    born = set(reg.session_ids("operator")) - before
    assert born, (
        "an operator's own '/session new' no longer opens a session — #3595 step 1b "
        "closed slash dispatch to kinds that are not the operator's, and must leave "
        "the operator's own path untouched"
    )
    assert scripted.calls == 0, (
        "the operator's slash line reached the model instead of the slash handler, so "
        f"the spawn above is not evidence the command ran (llm calls: {scripted.calls})"
    )
