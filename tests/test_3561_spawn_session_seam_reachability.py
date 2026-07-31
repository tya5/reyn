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

The second claim is a different site of the same primitive and the opposite verdict.
Crash recovery (``restore_all`` / ``_rewake_pipeline_runs``) re-creates a session
through ``spawn_session`` with no narrowing argument either, and that one is NOT a
gap: the session is re-created under its ORIGINAL sid, and the #2103-S1a layer is
keyed by sid on disk. ``test_recovery_recreated_session_keeps_its_persisted_narrowing``
measures that it really survives — the ``#2126`` comment inside
``spawn_session_recorded`` documents that construction-time resolution runs with
``sid=None`` and therefore ignores ``config.yaml``, which is exactly the shape a
re-woken session could have been reborn wide through, so the survival is a result and
not a deduction. Its negative control, ``test_recovery_without_a_persisted_narrowing_
restores_an_unnarrowed_session``, removes the persisted file and shows the same
assertion going the other way — without it, a permanently-denied tool would satisfy
the positive leg for a reason having nothing to do with recovery.

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

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
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


def _envelope_denials(session: Session) -> "list[str]":
    """The tool names this session's live capability ENVELOPE denies, read off the
    public status-bar read model (``capability_visibility_state``) rather than any
    private attribute — the same surface the operator's own status bar renders."""
    state = session.capability_visibility_state()
    return [
        entry["name"] for entry in state.get("denied_by_envelope", ())
        if entry.get("kind") == "tool"
    ]


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


# ── the recovery site: the sid-keyed layer really is re-attached ─────────────


async def _spawn_narrowed_and_persist(reg: AgentRegistry, tmp_path: Path) -> str:
    """Spawn a narrowed session through the recorded seam and wait for its
    per-session snapshot to land on disk — ``restore_all`` discovers spawned
    sessions BY that file, so a test that raced it would measure nothing at all."""
    routing = AuditOnlyNoSurface()
    sid = await reg.spawn_session_recorded(
        "worker", mode="persistent", narrowing={"tool_deny": [_DENIED_TOOL]},
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    session = reg.get_session("worker", sid)
    assert session is not None
    # Non-empty restored state is restore_all's own precondition for re-creating a
    # session (step 5 skips a snapshot with nothing stranded in it).
    await session._put_inbox("user", {"text": "stranded", "chain_id": "c3561"})
    await session.await_quiescent()
    snapshot = (
        tmp_path / ".reyn" / "agents" / "worker" / "state" / "sessions" / sid / "snapshot.json"
    )
    for _ in range(100):
        if snapshot.is_file():
            break
        await asyncio.sleep(0.05)
    assert snapshot.is_file(), "the spawned session's snapshot never reached disk"
    return sid


@pytest.mark.asyncio
async def test_recovery_recreated_session_keeps_its_persisted_narrowing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a session re-created by crash recovery through the SYNC
    ``spawn_session`` primitive is still inside the narrowing it was spawned with.

    ``restore_all`` (and its ``_rewake_pipeline_runs`` sibling) pass no ``narrowing``
    — there is no argument to pass — and re-enter under the ORIGINAL sid, which is
    what the #2103-S1a layer is keyed by. That the layer is therefore re-attached
    rather than lost is not deducible from the call: ``spawn_session_recorded``'s own
    ``#2126`` note records that construction-time resolution runs with ``sid=None``
    and ignores ``config.yaml``, which is why it re-injects explicitly and why a
    recovery path that does not could have been reborn wide.

    A fresh registry over the same tree is the restart: same project root, same WAL,
    nothing carried in memory.
    """
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    sid = await _spawn_narrowed_and_persist(reg, tmp_path)
    assert _DENIED_TOOL in _envelope_denials(reg.get_session("worker", sid)), (
        "precondition: the freshly spawned session is not narrowed, so the recovery "
        "assertion below would be vacuous"
    )

    restarted = _registry(tmp_path)
    await restarted.restore_all()

    recovered = restarted.get_session("worker", sid)
    assert recovered is not None, "crash recovery did not re-create the spawned session"
    assert _DENIED_TOOL in _envelope_denials(recovered), (
        "a session re-created by crash recovery was reborn OUTSIDE the per-session "
        "narrowing it was spawned with — the sid-keyed #2103-S1a layer did not "
        f"survive the restart (envelope denials: {_envelope_denials(recovered)!r})"
    )


@pytest.mark.asyncio
async def test_recovery_without_a_persisted_narrowing_restores_an_unnarrowed_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the negative control for the leg above — with the persisted narrowing
    removed, the same recovery path restores a session that does NOT deny the tool.

    Without this, the positive leg is satisfiable by a tool that is denied for some
    unrelated, permanent reason (a category default, a floor), which would make it
    green whether or not recovery carries anything. Deleting the one file the claim
    is about is the smallest change that separates the two explanations.
    """
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    sid = await _spawn_narrowed_and_persist(reg, tmp_path)

    config = tmp_path / ".reyn" / "agents" / "worker" / "state" / "sessions" / sid / "config.yaml"
    assert config.is_file(), "the recorded spawn seam did not persist a narrowing to read"
    config.unlink()

    restarted = _registry(tmp_path)
    await restarted.restore_all()

    recovered = restarted.get_session("worker", sid)
    assert recovered is not None, "crash recovery did not re-create the spawned session"
    assert _DENIED_TOOL not in _envelope_denials(recovered), (
        "the tool is denied even with no persisted narrowing, so the positive leg's "
        "green says nothing about recovery carrying the narrowing"
    )
