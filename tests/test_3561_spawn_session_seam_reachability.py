"""Tier 2: OS invariant — the two behavioural claims that put
``AgentRegistry.spawn_session`` inside the #3546/#3553/#3556 spawn-seam gate.

#3561. That primitive was outside the gate because it takes no ``narrowing``
argument, and the question asked about it was "is a primitive that cannot take a
narrowing in scope?" — a SHAPE question. It is the same question #3554's gate asked
in the opposite direction: #3554 counted a site as compliant BECAUSE it spelled
``narrowing=``, which #3556 satisfied while passing a value that was no function of
its parent at all. "Cannot inherit" is not "need not inherit": an API with no
inheritance channel is an unmet requirement, not an exemption. The criterion is
REACHABILITY — can a narrowed subject cause this to run.

★ **Reachability was measured before the site was classified, not after.** The
standing assumption was that ``AgentRegistry.spawn_session``'s one non-registry call
site, the ``/session new`` slash command, is operator-initiated and therefore out of
scope. The path that had actually been established was only that the OPERATOR input
face reaches it — never that no other face does.
``test_model_output_reaches_slash_dispatch_and_spawns_a_session`` is the falsification:
it drives the real ``run_agent_step`` with a prompt that is a previous agent step's
MODEL OUTPUT, on a session narrowed to a single capability, and observes a session
being born. The assumption is false.

Why the reaching turn is not a model turn: ``Session._handle_user_message`` tests
``text.startswith("/")`` and hands the line to ``_maybe_handle_slash`` BEFORE the
router turn, so the worker never calls its LLM on that turn at all — which the test
asserts, because "the scripted LLM was not consulted" is what distinguishes a slash
short-circuit from a model that happened to decide to spawn. Slash dispatch has
exactly two production callers (``_handle_user_message`` and
``maybe_deliver_answer_command``), so the surface reached here is small and
enumerable; what is NOT bounded is the set of ways text arrives at
``_handle_user_message``, and the agent-step prompt is one of them.

What this file deliberately does NOT assert: that the session ``/session new`` opens
is un-narrowed. It is (the invoking session's #2103-S1a layer is not carried, which is
#3562), but pinning that would make the fix red. The measurement is the reachability,
which stays true either way.

★ **The second claim is a defect this enumeration found, and the fix that closes it.**
Crash recovery (``restore_all`` / ``_rewake_pipeline_runs``) re-creates a session
through the SYNC ``spawn_session`` with no narrowing argument either — and until #3561
it was reborn WIDE. The session is re-created under its ORIGINAL sid and its
#2103-S1a ``config.yaml`` is still on disk, but the live session's
``_contextual_permission`` — the single source the RouterLoop's advertisement filter
and its ``_excluded_result`` call-time gate both read — was resolved by the factory
with ``sid=None`` and so never saw that file. ``spawn_session_recorded`` re-resolved
and re-injected WITH the sid (its ``#2126`` note says why); nothing on the direct path
to the primitive did. #3561 moved that injection into ``spawn_session`` itself, where
the sid becomes known, which closes it for every direct caller at once.

★ **The measurement that found it is also a lesson about which surface to read.** The
first attempt asserted on ``capability_visibility_state()`` — the operator's status
bar — and was GREEN on the broken code, because that surface re-resolves with the sid
on every read while the enforcement path does not. Two surfaces, one decorative. What
found the defect was ``write_file``'s real file landing on disk, next to a POSITIVE
CONTROL (``test_the_witness_tool_runs_when_nothing_narrows_it``) proving the witness
was alive at all: an earlier run of these legs had every arm "denied", including the
un-narrowed one, because the write gate — not the narrowing — was refusing everything.
The control is permanent for that reason.

The completeness half of the gate — which sites exist, how they are resolved, and
what each declares — lives in
``tests/test_3546_pipeline_driver_narrowing_inheritance.py``; this module is what its
``measured_by`` entries for the ``spawn_session`` sites point at.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import _interpolate_prompt
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_agent_step
from reyn.runtime.session_params import PresentationWiring
from reyn.runtime.spawn_routing import AuditOnlyNoSurface
from tests._support.agent_session import make_session
from tests._support.permissions import make_resolver

#: The line the scripted LLM emits. It is a real, registered slash command whose
#: handler calls ``AgentRegistry.spawn_session`` — see ``interfaces/slash/session.py``.
_MODEL_OUTPUT = "/session new"

#: The tool the recovery legs narrow away. A production, catalog-listed tool, so the
#: envelope read below is about a capability that really exists.
_DENIED_TOOL = "write_file"


class _ScriptedReply:
    """A real ``_llm_caller``-shaped callable answering with one fixed plain-text
    turn — the Tier-2c LLM stand-in this arc's sibling files already use (see
    ``tests/test_pipeline_r5_run_agent_step.py``), NOT a ``MagicMock``: a signature
    drift in the ``call_llm_tools`` contract raises ``TypeError`` here exactly as it
    would in production.

    ``calls`` is load-bearing, not diagnostic: the reachability leg reads it to show
    that the turn which reached slash dispatch consulted no model at all.
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
    tmp_path: Path, scripted: "_ScriptedReply | None" = None, *, agents: "tuple[str, ...]" = ("worker",),
) -> AgentRegistry:
    """Real ``AgentRegistry`` + real ``Session`` factory — the harness shape
    ``tests/test_pipeline_r5_run_agent_step.py`` uses, including the ``holder``
    deferred-registry-ref so the factory can pass ``registry=`` for ephemeral
    auto-vanish, and its real-litellm-name resolver so a spawned session's
    model-support pre-check has a name to resolve."""
    if not (tmp_path / "reyn.yaml").exists():
        (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}
    resolver = ModelResolver({"standard": "gemini/gemini-2.5-flash-lite"})
    # ``file.write`` approved at the PERMISSION layer, so a write that does not happen
    # below is the capability narrowing refusing it and not the write gate — the
    # confusion that made an earlier version of the recovery legs read "denied"
    # everywhere, including the un-narrowed control.
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
        if scripted is not None:
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



# ── the reachability falsification ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_output_reaches_slash_dispatch_and_spawns_a_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: MODEL OUTPUT reaches slash dispatch, and through it
    ``AgentRegistry.spawn_session`` — the measurement that decides
    ``interfaces/slash/session.py::session_cmd`` is a spawn seam in scope.

    Two agent steps, both real ``run_agent_step`` calls on real sessions:

      1. the first returns the model's text — here a slash command;
      2. the production template splice (``executor._interpolate_prompt``, the exact
         function an ``agent`` step's ``prompt`` goes through) puts that text in the
         second step's prompt;
      3. the second step's worker — narrowed to a single capability, so it is a
         NARROWED subject — reaches ``/session new`` and a session is born under the
         attached agent.

    No operator submits anything at any point. ``scripted.calls`` staying at 1 across
    step 2 is what shows the reaching turn short-circuited at
    ``_maybe_handle_slash`` instead of running a router turn: without it, a spawn
    could be explained by the worker's own model deciding to spawn, which is a
    different (already-gated, #3556) path.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _ScriptedReply(_MODEL_OUTPUT)
    reg = _registry(tmp_path, scripted, agents=("worker", "operator"))

    # 1. a model emits a slash command as its ordinary reply text.
    model_output = await run_agent_step(reg, identity="worker", prompt="go")
    assert model_output == _MODEL_OUTPUT
    assert scripted.calls == 1

    # 2. the production splice puts it in the next step's prompt, verbatim.
    next_prompt = _interpolate_prompt("{pipe}", {"ctx": {}, "pipe": model_output})
    assert next_prompt == _MODEL_OUTPUT

    # An operator is attached, which is the ordinary REPL state — ``/session new``
    # acts on the ATTACHED agent, so this is whose session count moves.
    reg.get_or_load("operator")
    await reg.attach_session("operator", "main")
    before = set(reg.session_ids("operator"))

    # 3. a narrowed worker runs that prompt.
    await run_agent_step(
        reg, identity="worker", prompt=next_prompt, capabilities=[_DENIED_TOOL],
    )

    born = set(reg.session_ids("operator")) - before
    assert born, (
        "model output did not reach slash dispatch — no session was born under the "
        "attached agent. If this is now correct, the reachability claim in "
        "tests/test_3546_pipeline_driver_narrowing_inheritance.py's declaration for "
        "interfaces/slash/session.py::session_cmd is stale and must be re-argued."
    )
    assert scripted.calls == 1, (
        "the reaching turn consulted the LLM, so the spawn is not evidence of the "
        "slash short-circuit — _handle_user_message is expected to hand a '/'-prefixed "
        f"line to _maybe_handle_slash before any router turn (llm calls: {scripted.calls})"
    )




# ── the recovery site: the sid-keyed layer must survive re-creation ──────────

#: The file the witness tool writes. Relative, so it lands wherever the session's
#: workspace resolves it and ``rglob`` finds it either way.
_OUT_NAME = "p3561_out.txt"


class _WritesOnceLLM:
    """A real ``_llm_caller``-shaped callable: the FIRST turn asks for
    ``_DENIED_TOOL``, every later turn answers in plain text so the loop terminates
    whether or not the call was allowed. Not a ``MagicMock`` — a drift in the
    ``call_llm_tools`` keyword contract raises ``TypeError`` here as in production.

    ``turns`` is load-bearing: an absence on disk observed because the session never
    took a turn is indistinguishable from one observed because the capability was
    denied, so every leg asserts the turn happened before reading the filesystem.
    """

    def __init__(self) -> None:
        self.turns = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.turns += 1
        if self.turns == 1:
            return LLMToolCallResult(
                content=None,
                tool_calls=[{
                    "id": "c3561", "type": "function",
                    "function": {
                        "name": _DENIED_TOOL,
                        "arguments": '{"path": "%s", "content": "ran"}' % _OUT_NAME,
                    },
                }],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            )
        return LLMToolCallResult(
            content="done", tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _written(tmp_path: Path) -> "list[str]":
    """Every on-disk copy of the witness file — the side effect itself, never a
    status string."""
    return sorted(str(p) for p in tmp_path.rglob(_OUT_NAME))


async def _drive_one_turn(session: Session) -> None:
    """Run ONE real turn on ``session`` through the production run+collect primitive
    (``MessageBus.request``), the same way ``run_agent_step`` drives a worker."""
    from reyn.runtime.message_bus import MessageBus
    from reyn.runtime.transport import SystemRef

    await MessageBus().request(
        session, kind="user", payload={"text": "write it", "chain_id": "c3561t"},
        reply_to=SystemRef(), timeout=30,
    )


async def _spawn_and_persist(
    reg: AgentRegistry, tmp_path: Path, narrowing: "dict | None",
) -> str:
    """Spawn a session through the recorded seam and wait for its per-session
    snapshot to land on disk — ``restore_all`` discovers spawned sessions BY that
    file, so a test that raced it would restore nothing and measure nothing."""
    routing = AuditOnlyNoSurface()
    sid = await reg.spawn_session_recorded(
        "worker", mode="persistent", narrowing=narrowing,
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    session = reg.get_session("worker", sid)
    assert session is not None
    # Non-empty restored state is restore_all's own precondition for re-creating a
    # session (its step 5 skips a snapshot with nothing stranded in it).
    await session._put_inbox("user", {"text": "stranded", "chain_id": "c3561s"})
    await session.await_quiescent()
    snapshot = (
        tmp_path / ".reyn" / "agents" / "worker" / "state" / "sessions" / sid / "snapshot.json"
    )
    for _ in range(200):
        if snapshot.is_file():
            break
        await asyncio.sleep(0.05)
    assert snapshot.is_file(), "the spawned session's snapshot never reached disk"
    return sid


@pytest.mark.asyncio
async def test_the_witness_tool_runs_when_nothing_narrows_it(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the POSITIVE CONTROL for the leg below — with no narrowing anywhere,
    the recovery-recreated session's ``write_file`` really does reach its handler and
    put a file on disk.

    This is not ceremony. The first version of these legs reported "denied" on every
    arm INCLUDING this one, because ``file.write`` was refused by the permission
    resolver rather than by any narrowing — an absence that proved nothing while
    looking exactly like proof. A leg that observes a side effect NOT happening is
    only readable next to one that observes it happening.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM()
    reg = _registry(tmp_path, scripted)
    sid = await _spawn_and_persist(reg, tmp_path, narrowing=None)

    restarted = _registry(tmp_path, scripted)
    await restarted.restore_all()
    recovered = restarted.get_session("worker", sid)
    assert recovered is not None, "crash recovery did not re-create the spawned session"

    await _drive_one_turn(recovered)

    assert scripted.turns >= 2, (
        f"the recovered session never finished a turn (turns={scripted.turns}) — the "
        "leg below would then be asserting an absence caused by nothing running"
    )
    assert _written(tmp_path), (
        "the witness tool did not execute even with nothing narrowing it, so an "
        "absence observed in the narrowed leg would say nothing about the narrowing"
    )


@pytest.mark.asyncio
async def test_recovery_recreated_session_is_still_inside_its_persisted_narrowing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a session re-created by crash recovery through the SYNC
    ``spawn_session`` primitive cannot execute a capability its persisted per-session
    narrowing denies.

    ``restore_all`` passes no ``narrowing`` — there is no argument to pass — and
    re-enters under the ORIGINAL sid, which is what the #2103-S1a layer is keyed by.
    Before #3561 that layer was resolvable but not ENFORCED: the factory resolves an
    envelope with ``sid=None``, so the live ``_contextual_permission`` the RouterLoop
    reads never saw the sid's ``config.yaml``, and the re-woken session wrote the file.
    The assertion is the file's absence, next to
    ``test_the_witness_tool_runs_when_nothing_narrows_it``'s presence.

    A fresh registry over the same tree is the restart: same project root, same WAL,
    nothing carried in memory.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM()
    reg = _registry(tmp_path, scripted)
    sid = await _spawn_and_persist(reg, tmp_path, narrowing={"tool_deny": [_DENIED_TOOL]})

    restarted = _registry(tmp_path, scripted)
    await restarted.restore_all()
    recovered = restarted.get_session("worker", sid)
    assert recovered is not None, "crash recovery did not re-create the spawned session"

    await _drive_one_turn(recovered)

    assert scripted.turns >= 1, (
        "the recovered session never took a turn, so the absence below is vacuous"
    )
    assert not _written(tmp_path), (
        "a session re-created by crash recovery executed a tool its own persisted "
        "per-session narrowing denies — the sid-keyed #2103-S1a layer was resolvable "
        f"but not enforced after the restart (written: {_written(tmp_path)!r})"
    )
