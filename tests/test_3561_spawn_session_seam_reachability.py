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
face reaches it — never that no other face does. The original
``test_model_output_reaches_slash_dispatch_and_spawns_a_session`` was the
falsification: it drove the real ``run_agent_step`` with a prompt that is a previous
agent step's MODEL OUTPUT, on a session narrowed to a single capability, and observed
a session being born. The assumption was false.

★ **#3595 step 1 made it true, and this file's leg is the same measurement inverted.**
The reason model output reached slash dispatch was a KIND that was not true of it:
``run_agent_step`` fed its prompt as ``kind="user"``, i.e. as a line a human had typed
at a client, and ``Session._handle_user_message`` acts on that claim by handing a
``/``-prefixed line to ``_maybe_handle_slash`` before any router turn. The prompt now
rides its own union member (``TurnOrigin.AGENT_STEP``), which ``_run_turn_body``
routes straight to the shared turn body — the slash dispatch is not
skipped by a flag, it is not on that path at all. The leg below asserts the ABSENCE of
the spawn, and asserts the scripted LLM WAS consulted on the reaching turn: the text
is now content a model reads, which is the positive half that keeps the absence from
being explained by "the worker never ran". ★ The original assertion message asked for
exactly this re-argument if the reachability claim ever became false; the guard is
kept alive inverted rather than deleted, because what has to stay measured is that no
non-operator face reaches an OS-executed command — a claim that only an executable
witness can hold. The paired operator leg
(``test_an_operator_submitted_slash_command_still_spawns_a_session``) is its control:
the same command, submitted the way every client submits one, still executes, so
"nothing spawns any more" cannot pass for the fix.

Slash dispatch has exactly two production callers (``_handle_user_message`` and
``maybe_deliver_answer_command``), so the surface is small and enumerable; what is NOT
bounded is the set of ways text arrives at ``_handle_user_message`` — which is why the
fix was to bound the KIND that may enter it rather than to enumerate the commands.

What this file deliberately does NOT assert: anything about the narrowing of the
session ``/session new`` opens (#3562's subject). The measurement is WHO can reach the
site, which is a separate axis from what the child inherits when they do.

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

★ **The third claim (#3564) is the truncate-falsify witness CLAUDE.md's recovery-feature
gate asks for.** #3561 changed a crash-recovery path and carried no truncate leg. The
exemption is real — the narrowing's reconstruction source is the per-session
``config.yaml``, not a WAL-event, so "WAL-event-derived state that isn't snapshot-backed"
does not describe it — but an exemption recorded as an ABSENCE is indistinguishable from a
forgotten test to the next auditor. The two legs below rewrite the WAL past the
``session_spawned`` record that carried the narrowing (the WAL's only copy of it), then
restore, and observe the narrowing still enforced. Expected GREEN: this is the proof of
the exemption, not a retraction of it. The truncation is asserted to have removed that
record — a no-op rewrite would witness nothing — and the un-narrowed twin
(``test_a_truncated_wal_still_lets_the_witness_tool_run_when_nothing_narrows_it``) is
there because after a rewrite, "the file is absent" could otherwise be explained by the
truncation having broken recovery outright rather than by the narrowing holding.

The completeness half of the gate — which sites exist, how they are resolved, and
what each declares — lives in
``tests/test_3546_pipeline_driver_narrowing_inheritance.py``; this module is what its
``measured_by`` entries for the ``spawn_session`` sites point at.
"""
from __future__ import annotations

import asyncio
import uuid
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



# ── the reachability measurement, inverted by #3595 step 1 ──────────────────


@pytest.mark.asyncio
async def test_model_output_cannot_reach_slash_dispatch_and_spawns_nothing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: MODEL OUTPUT does not reach slash dispatch, so it cannot reach
    ``AgentRegistry.spawn_session`` through ``interfaces/slash/session.py::session_cmd``.

    The #3561 falsification, run unchanged and asserted the other way round (#3595
    step 1). Two agent steps, both real ``run_agent_step`` calls on real sessions:

      1. the first returns the model's text — here a real, registered slash command;
      2. the production template splice (``executor._interpolate_prompt``, the exact
         function an ``agent`` step's ``prompt`` goes through) puts that text in the
         second step's prompt;
      3. the second step's worker runs that prompt and NO session is born.

    The second assertion is what makes the absence readable. ``scripted.calls``
    reaching 2 means the reaching turn went to the model — the slash-shaped line was
    delivered as CONTENT for a router turn. Without it, "no session was born" would
    have a second explanation (the worker never ran at all), which is the shape an
    absence-only leg cannot tell apart. The two together say: the line arrived, and it
    arrived as text rather than as a command.

    The invariant is not about ``/session new``. It is that a non-operator inbox kind
    cannot execute ANY of the registered slash commands, because the kind that carries
    it never enters the dispatch — ``/session new`` is simply the command whose side
    effect is observable from outside the session.
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
    # acts on the ATTACHED agent, so this is whose session count would move.
    reg.get_or_load("operator")
    await reg.attach_session("operator", "main")
    before = set(reg.session_ids("operator"))

    # 3. a narrowed worker runs that prompt.
    await run_agent_step(
        reg, identity="worker", prompt=next_prompt, capabilities=[_DENIED_TOOL],
    )

    born = set(reg.session_ids("operator")) - before
    assert not born, (
        "model output reached slash dispatch and spawned a session under the attached "
        f"agent ({sorted(born)!r}) — the agent-step prompt is being interpreted as an "
        "operator command line again, which puts all 25 registered slash commands back "
        "within reach of model output (#3595 step 1: the prompt must ride "
        "TurnOrigin.AGENT_STEP, never kind='user')"
    )
    assert scripted.calls == 2, (
        "the reaching turn never consulted the LLM, so the absence above is not "
        "evidence that the prompt was delivered as content — the worker may not have "
        f"taken a turn at all (llm calls: {scripted.calls})"
    )


@pytest.mark.asyncio
async def test_an_operator_submitted_slash_command_still_spawns_a_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the CONTROL for the leg above — an operator-submitted ``/session new``
    still executes, so slash dispatch is closed to model output and not to everyone.

    Same registry, same command, same observable (a session born under the attached
    agent); the only difference is the door the text comes through
    (``Session.submit_user_text`` — the one public entry every client's composer ends
    at, CUI and TUI alike). Without this leg, deleting slash dispatch outright would
    also pass the absence above, and #3595 step 1 explicitly must not change what an
    operator typing ``/model foo`` gets.

    ``scripted.calls`` staying at 0 is the other half: the operator's line
    short-circuited at ``_maybe_handle_slash`` before any router turn, which is what
    distinguishes an executed command from a model that decided to spawn.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _ScriptedReply("nothing to say")
    reg = _registry(tmp_path, scripted, agents=("operator",))

    operator = reg.get_or_load("operator")
    await reg.attach_session("operator", "main")
    before = set(reg.session_ids("operator"))

    await operator.submit_user_text(_MODEL_OUTPUT)
    await operator.run_one_iteration()
    await operator.await_quiescent()

    born = set(reg.session_ids("operator")) - before
    assert born, (
        "an operator's own '/session new' no longer opens a session — #3595 step 1 "
        "closed slash dispatch to a kind that is not the operator's, and must leave "
        "the operator's own path untouched"
    )
    assert scripted.calls == 0, (
        "the operator's slash line reached the model instead of the slash handler, so "
        f"the spawn above is not evidence the command ran (llm calls: {scripted.calls})"
    )




# ── the recovery site: the sid-keyed layer must survive re-creation ──────────

def _out_name() -> str:
    """A witness filename unique to ONE test run.

    Not decoration. The witness is searched for across the whole per-worker tmp tree,
    not just this test's ``tmp_path``, because a session's workspace does not
    necessarily resolve where the test's cwd points — and an earlier version of these
    legs went green on BROKEN code for exactly that reason: run after a sibling test in
    the same process, the denied write landed under the sibling's directory, so
    ``tmp_path.rglob`` found nothing and the absence read as a denial. Unique name plus
    wide search means a write that happens ANYWHERE is attributed to the run that
    caused it, and the leg fails as it should. (Run alone, the same test failed — which
    is how the discrepancy surfaced. A gate that only fires in a cold process is not a
    gate.)
    """
    return f"p3561_out_{uuid.uuid4().hex}.txt"


class _WritesOnceLLM:
    """A real ``_llm_caller``-shaped callable: the FIRST turn asks for
    ``_DENIED_TOOL``, every later turn answers in plain text so the loop terminates
    whether or not the call was allowed. Not a ``MagicMock`` — a drift in the
    ``call_llm_tools`` keyword contract raises ``TypeError`` here as in production.

    ``turns`` is load-bearing: an absence on disk observed because the session never
    took a turn is indistinguishable from one observed because the capability was
    denied, so every leg asserts the turn happened before reading the filesystem.
    """

    def __init__(self, out_name: str) -> None:
        self.out_name = out_name
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
                        "arguments": '{"path": "%s", "content": "ran"}' % self.out_name,
                    },
                }],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            )
        return LLMToolCallResult(
            content="done", tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _written(tmp_path: Path, out_name: str) -> "list[str]":
    """Every on-disk copy of the witness file — the side effect itself, never a status
    string — searched from the per-worker tmp ROOT, not from this test's own directory.
    See :func:`_out_name` for why the wider search is the point."""
    return sorted(str(p) for p in tmp_path.parent.rglob(out_name))


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


async def _truncate_past_the_spawn_record(reg: AgentRegistry, sid: str) -> None:
    """Rewrite the WAL keeping only entries ABOVE ``sid``'s ``session_spawned`` record —
    the WAL's one carrier of the spawn-time narrowing — and prove the rewrite really
    removed it.

    The proof is the point (#3564): a truncate leg whose truncation dropped nothing is
    green for a reason that has nothing to do with what it claims to measure. So this
    asserts the record was present before, that something was dropped, and that its seq is
    gone afterwards — read through ``last_truncate_stats`` and a re-read of the file, both
    public.

    The floor is ``spawn_seq + 1`` rather than "everything": entries ABOVE the spawn
    record carry the session's stranded inbox state, which is ``restore_all``'s own
    precondition for re-creating the session at all (see :func:`_spawn_and_persist`).
    Dropping those would leave nothing to restore and the leg would measure nothing.
    """
    log = reg.state_log
    assert log is not None
    before = list(log.iter_from(0))
    spawn_seqs = [
        int(e["seq"]) for e in before
        if e.get("kind") == "session_spawned" and e.get("sid") == sid
    ]
    assert spawn_seqs, (
        "no session_spawned record for this sid is in the WAL, so there is nothing "
        "preceding the narrowing to truncate past and the leg would witness nothing"
    )
    spawn_seq = max(spawn_seqs)
    assert any(int(e["seq"]) > spawn_seq for e in before), (
        "the session_spawned record is the WAL's highest entry, and the rewrite never "
        "drops the highest seq (it would let the next _scan_max_seq re-issue used seqs) "
        "— so the truncation below would keep the very record it must remove"
    )

    await log.truncate_below(spawn_seq + 1)
    await log.flush()  # the rewrite is fire-and-forget; this is its barrier.

    stats = log.last_truncate_stats
    assert stats["dropped"] > 0, (
        f"the WAL rewrite dropped nothing ({stats!r}) — a no-op truncation proves "
        "nothing about what survives one"
    )
    surviving = {int(e["seq"]) for e in log.iter_from(0)}
    assert spawn_seq not in surviving, (
        f"the session_spawned record (seq={spawn_seq}) survived the rewrite, so the "
        "restore below still has the WAL's copy of the narrowing available to it"
    )
    assert min(surviving) > spawn_seq, (
        f"entries at or below the spawn record survived ({stats!r}) — the truncation "
        "did not reach past the point the narrowing was established"
    )


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
    out_name = _out_name()
    scripted = _WritesOnceLLM(out_name)
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
    assert _written(tmp_path, out_name), (
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
    out_name = _out_name()
    scripted = _WritesOnceLLM(out_name)
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
    assert not _written(tmp_path, out_name), (
        "a session re-created by crash recovery executed a tool its own persisted "
        "per-session narrowing denies — the sid-keyed #2103-S1a layer was resolvable "
        f"but not enforced after the restart (written: {_written(tmp_path, out_name)!r})"
    )


# ── the truncate-falsify witness (#3564) ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_truncated_wal_still_lets_the_witness_tool_run_when_nothing_narrows_it(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the POSITIVE CONTROL for the truncate leg below — after the WAL is
    rewritten past the session's spawn record, an UN-narrowed recovery-recreated session
    still reaches its ``write_file`` handler and puts a file on disk.

    Without this twin, the absence asserted below would have a second available
    explanation: that truncating the WAL broke recovery (or the turn, or the write path)
    outright, so nothing wrote for reasons unrelated to any narrowing. Here the same
    rewrite happens and the file DOES land.
    """
    monkeypatch.chdir(tmp_path)
    out_name = _out_name()
    scripted = _WritesOnceLLM(out_name)
    reg = _registry(tmp_path, scripted)
    sid = await _spawn_and_persist(reg, tmp_path, narrowing=None)

    await _truncate_past_the_spawn_record(reg, sid)

    restarted = _registry(tmp_path, scripted)
    await restarted.restore_all()
    recovered = restarted.get_session("worker", sid)
    assert recovered is not None, (
        "crash recovery did not re-create the spawned session after the WAL rewrite — "
        "the leg below would then be asserting an absence caused by a broken restore"
    )

    await _drive_one_turn(recovered)

    assert scripted.turns >= 2, (
        f"the recovered session never finished a turn (turns={scripted.turns}) after the "
        "WAL rewrite, so the absence below would say nothing about the narrowing"
    )
    assert _written(tmp_path, out_name), (
        "with the WAL truncated past its spawn record and nothing narrowing it, the "
        "witness tool still did not execute — an absence in the narrowed leg would then "
        "be attributable to the truncation rather than to the narrowing"
    )


@pytest.mark.asyncio
async def test_persisted_narrowing_survives_a_wal_truncation_past_its_spawn_record(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the per-session narrowing enforced on a recovery-recreated session
    survives a WAL rewrite that drops the ``session_spawned`` record carrying it.

    CLAUDE.md's recovery-feature gate asks any PR that changes a reconstruction path for
    exactly this shape: set X → truncate past X's WAL-events → reconstruct → assert X
    survives. #3561 changed one (``restore_all`` / ``_rewake_pipeline_runs`` now re-enter
    the narrowing at ``spawn_session``) and carried no such leg, on the judgement that the
    rule's target — WAL-event-derived state with no snapshot behind it — does not describe
    this state: the reconstruction source is the sid's ``config.yaml`` under the
    per-session state dir, which a WAL rewrite cannot touch. ★ That judgement was recorded
    only as the ABSENCE of a test, which reads identically to a forgotten one. This leg is
    the judgement made checkable; GREEN is the expected and correct result.

    The rewrite is not decoration: :func:`_truncate_past_the_spawn_record` asserts the
    ``session_spawned`` entry existed and that its seq is gone afterwards, so the restore
    below genuinely has no WAL copy of the narrowing left to read. The witness is again
    the real ``write_file`` side effect — not ``capability_visibility_state()``, which
    re-resolves per read and was green on the broken code #3561 fixed.
    """
    monkeypatch.chdir(tmp_path)
    out_name = _out_name()
    scripted = _WritesOnceLLM(out_name)
    reg = _registry(tmp_path, scripted)
    sid = await _spawn_and_persist(reg, tmp_path, narrowing={"tool_deny": [_DENIED_TOOL]})

    await _truncate_past_the_spawn_record(reg, sid)

    restarted = _registry(tmp_path, scripted)
    await restarted.restore_all()
    recovered = restarted.get_session("worker", sid)
    assert recovered is not None, (
        "crash recovery did not re-create the spawned session after the WAL rewrite"
    )

    await _drive_one_turn(recovered)

    assert scripted.turns >= 1, (
        "the recovered session never took a turn, so the absence below is vacuous"
    )
    assert not _written(tmp_path, out_name), (
        "a session re-created after the WAL was truncated past its spawn record executed "
        "a tool its persisted per-session narrowing denies — the config.yaml-backed "
        "#2103-S1a layer did NOT survive the rewrite, which is the failure mode the "
        f"recovery-feature gate exists to catch (written: {_written(tmp_path, out_name)!r})"
    )
