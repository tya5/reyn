"""Tier 2: OS invariant — a session opened by ``/session new`` is born INSIDE the
invoking session's per-session capability narrowing.

#3562, the gap #3561 declared and did not close. ``interfaces/slash/session.py::
session_cmd`` calls ``AgentRegistry.spawn_session`` — the sync primitive, which until
this change had no ``narrowing`` channel at all — and carried nothing from its caller,
so the child's #2103-S1a layer was empty however narrow its invoker was.

★ **Why this file exists is an OWNER POLICY decision, not a containment argument, and
the difference is load-bearing enough to state before the tests.** The gap was first
argued as a security one: #3561 had measured that ``/session new`` was reachable from
MODEL OUTPUT (an agent step's prompt arrived as ``kind="user"``, so a ``/``-prefixed
line short-circuited to ``_maybe_handle_slash`` before any router turn), which made an
un-narrowed child an escape. ★ #3595 step 1 ruled that reachability a DEFECT and closed
it: the agent-step prompt now rides its own inbox kind, and
``tests/test_3561_spawn_session_seam_reachability.py`` measures its absence
(``test_model_output_cannot_reach_slash_dispatch_and_spawns_nothing``) next to an
operator control. So the escape argument is retired, and this file does not rest on it.

What remains, and what these tests measure, is the owner's ruling on the plain
question — if an operator has switched ``write_file`` off for their session and then
opens a new one from it, should ``write_file`` work there? — answered no. A session
opened from a narrowed session stays narrowed. That claim is about operator intent
persisting across a spawn, and it is unaffected by who can reach the command.

The composition is the three sibling sites' rule
(``capability_profile.compose_narrowing_mappings``: deny ∪, allow ∩, an absent allow
key = ⊤), restrict-only in every direction, applied UNIFORMLY — no branch for the case
where the invoking session's agent differs from the ATTACHED agent the child is born
under. ``name = reg.attached_name``, so on the operator path the caller IS the attach
target and the identities coincide; there is no live cross-identity case to branch on,
and a branch would be a lenient special case for exactly the caller a uniform
restrict-only rule exists to bound.

★ **What is the witness, and what is not.** The two behavioural legs below assert on
``write_file``'s REAL side effect — a file on disk — next to a positive control that
observes the same tool running when nothing narrows it. They deliberately do NOT read
``capability_visibility_state()``: that surface re-resolves with the sid on every read
and was GREEN on the broken code #3561 fixed, where the envelope was resolvable but not
enforced. The reply line this PR adds is rendered FROM that same surface, so it is an
explanation for the operator, not evidence that anything is enforced — it gets its own
test (``test_the_reply_names_what_the_new_session_inherited``) for its own, separate
claim.

The user-visible consequence is real and intended (owner-approved): an operator whose
own session is narrowed now gets a narrowed new session. The asymmetry behind it is
recoverability — an over-narrow child announces itself (something is refused, and the
operator can re-open from an unnarrowed session), whereas the over-wide child the old
code produced announced nothing at all.

The completeness half of this gate — which spawn sites exist and what each declares —
lives in ``tests/test_3546_pipeline_driver_narrowing_inheritance.py``; this module is
what its declaration for ``interfaces/slash/session.py::session_cmd`` now points at.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.runtime.message_bus import MessageBus
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import PresentationWiring
from reyn.runtime.spawn_routing import AuditOnlyNoSurface
from reyn.runtime.transport import SystemRef
from tests._support.agent_session import make_session
from tests._support.permissions import make_resolver

#: The capability the invoking session is narrowed away from. A production,
#: catalog-listed tool whose execution leaves an observable trace on disk.
_DENIED_TOOL = "write_file"

#: The agent whose session invokes ``/session new``.
_INVOKER_AGENT = "worker"

#: The ATTACHED agent — the one ``/session new`` opens the child under. Different from
#: the invoker on purpose: it is the cross-identity shape, the one the composition must
#: NOT branch on, and driving it here means the uniform rule is exercised rather than
#: assumed from the same-identity case that an operator's own keystroke produces.
_ATTACHED_AGENT = "operator"


def _out_name() -> str:
    """A witness filename unique to ONE test run.

    Searched for across the whole per-worker tmp tree rather than this test's own
    ``tmp_path`` — a session's workspace does not necessarily resolve where the test's
    cwd points, and #3561 recorded an earlier version of a sibling leg going green on
    BROKEN code for exactly that reason (the denied write landed under a sibling test's
    directory, so a narrow search found nothing and the absence read as a denial).
    """
    return f"p3562_out_{uuid.uuid4().hex}.txt"


class _WritesOnceLLM:
    """A real ``_llm_caller``-shaped callable: the FIRST turn asks for ``_DENIED_TOOL``,
    every later turn answers in plain text so the loop terminates whether or not the
    call was allowed. Not a ``MagicMock`` — a drift in the ``call_llm_tools`` keyword
    contract raises ``TypeError`` here exactly as it would in production.

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
                    "id": "c3562", "type": "function",
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
    string — searched from the per-worker tmp ROOT. See :func:`_out_name`."""
    return sorted(str(p) for p in tmp_path.parent.rglob(out_name))


def _registry(tmp_path: Path, scripted: "_WritesOnceLLM") -> AgentRegistry:
    """Real ``AgentRegistry`` + real ``Session`` factory — the harness shape
    ``tests/test_3561_spawn_session_seam_reachability.py`` uses, including the
    ``holder`` deferred-registry-ref (so a spawned session gets ``registry=``) and a
    real-litellm-name resolver (so a spawned session's model-support pre-check has a
    name to resolve).

    ``file.write`` is approved at the PERMISSION layer, so a write that does not happen
    is the capability narrowing refusing it and not the write gate — the confusion that
    made an earlier version of #3561's legs read "denied" everywhere, including the
    un-narrowed control.
    """
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
    for name in (_INVOKER_AGENT, _ATTACHED_AGENT):
        if not reg.exists(name):
            reg.create(name)
    return reg


async def _drive(session: Session, text: str) -> "list[OutboxMessage]":
    """Run ONE real turn on ``session`` through the production run+collect primitive
    (``MessageBus.request``). Returns the turn's outbox messages, which is where a slash
    reply lands.

    ★ ``kind="user"`` is a deliberate claim, not a convenience. Since #3595 step 1 that
    kind means specifically "an operator typed this at a client", and it is the only one
    whose text ``Session._handle_user_message`` interprets as a command surface before
    handing it to the shared turn body (``_handle_inbox_text``). Simulating an operator
    keystroke is exactly what these legs need — the ONLY face that reaches ``/session
    new`` today — so this is the right side of that split. A producer that is not an
    operator would have to call the lower half directly and could not run the command at
    all, which is ``tests/test_3561_spawn_session_seam_reachability.py``'s subject."""
    return await MessageBus().request(
        session, kind="user", payload={"text": text, "chain_id": "c3562t"},
        reply_to=SystemRef(), timeout=30,
    )


async def _invoker(
    reg: AgentRegistry, narrowing: "dict | None",
) -> Session:
    """The session that will type ``/session new`` — spawned under ``_INVOKER_AGENT``
    with (or without) a per-session narrowing of its own, through the recorded seam that
    is the production writer of that layer."""
    routing = AuditOnlyNoSurface()
    sid = await reg.spawn_session_recorded(
        _INVOKER_AGENT, mode="persistent", narrowing=narrowing,
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    session = reg.get_session(_INVOKER_AGENT, sid)
    assert session is not None
    return session


async def _open_child(reg: AgentRegistry, invoker: Session) -> "tuple[str, list[OutboxMessage]]":
    """Drive ``/session new`` on ``invoker`` and return the sid of the session that was
    born under the ATTACHED agent, plus the turn's outbox messages.

    Fails loudly if no session appeared: every leg below is about the child's envelope,
    and a leg that measured a child which does not exist would be vacuous.
    """
    reg.get_or_load(_ATTACHED_AGENT)  # attach_session focuses, it never builds
    await reg.attach_session(_ATTACHED_AGENT, "main")
    before = set(reg.session_ids(_ATTACHED_AGENT))
    replies = await _drive(invoker, "/session new")
    born = set(reg.session_ids(_ATTACHED_AGENT)) - before
    assert len(born) == 1, (
        f"/session new did not open exactly one session under {_ATTACHED_AGENT!r} "
        f"(new: {sorted(born)!r}) — the leg would then be measuring nothing"
    )
    return born.pop(), replies


@pytest.mark.asyncio
async def test_the_witness_tool_runs_when_the_invoker_is_not_narrowed(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the POSITIVE CONTROL — a session opened by ``/session new`` from an
    UN-narrowed invoker really does reach ``write_file``'s handler and put a file on
    disk.

    Not ceremony. #3561 recorded a version of these legs reporting "denied" on every arm
    including the control, because ``file.write`` was refused by the permission resolver
    rather than by any narrowing — an absence that proved nothing while looking exactly
    like proof. The leg below observes a side effect NOT happening; it is only readable
    next to one that observes it happening.
    """
    monkeypatch.chdir(tmp_path)
    out_name = _out_name()
    scripted = _WritesOnceLLM(out_name)
    reg = _registry(tmp_path, scripted)

    invoker = await _invoker(reg, narrowing=None)
    child_sid, _ = await _open_child(reg, invoker)
    child = reg.get_session(_ATTACHED_AGENT, child_sid)
    assert child is not None

    await _drive(child, "write it")

    assert scripted.turns >= 2, (
        f"the child session never finished a turn (turns={scripted.turns}) — the leg "
        "below would then be asserting an absence caused by nothing running"
    )
    assert _written(tmp_path, out_name), (
        "the witness tool did not execute even with nothing narrowing it, so an absence "
        "observed in the narrowed leg would say nothing about the narrowing"
    )


@pytest.mark.asyncio
async def test_a_session_opened_by_slash_session_new_cannot_run_a_tool_its_invoker_is_denied(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a session opened by ``/session new`` from a NARROWED invoker cannot
    execute a capability that invoker is denied.

    The invoker is a session of one agent and the child is born under a DIFFERENT
    (attached) agent — the cross-identity shape — and the narrowing is carried anyway.
    That is the uniform rule being exercised rather than assumed: the same-identity case
    an operator's own keystroke produces would pass under a lenient special case too, so
    the leg that distinguishes them is this one.

    The assertion is the file's ABSENCE, next to
    ``test_the_witness_tool_runs_when_the_invoker_is_not_narrowed``'s presence. Before
    #3562 the same setup wrote the file: the child's #2103-S1a layer was empty, so the
    RouterLoop's advertisement filter and its ``_excluded_result`` call-time gate had
    nothing to read.
    """
    monkeypatch.chdir(tmp_path)
    out_name = _out_name()
    scripted = _WritesOnceLLM(out_name)
    reg = _registry(tmp_path, scripted)

    invoker = await _invoker(reg, narrowing={"tool_deny": [_DENIED_TOOL]})
    child_sid, _ = await _open_child(reg, invoker)
    child = reg.get_session(_ATTACHED_AGENT, child_sid)
    assert child is not None

    await _drive(child, "write it")

    assert scripted.turns >= 1, (
        "the child session never took a turn, so the absence below is vacuous"
    )
    assert not _written(tmp_path, out_name), (
        "a session opened by /session new executed a tool the session that opened it is "
        "denied — the invoker's per-session narrowing was not carried into the child's "
        f"envelope (written: {_written(tmp_path, out_name)!r})"
    )


@pytest.mark.asyncio
async def test_the_reply_names_what_the_new_session_inherited(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the ``/session new`` reply tells the operator which capabilities the new
    session inherited a denial of.

    A SEPARATE claim from the two legs above, deliberately: with ``allow ∩`` composed
    uniformly, a child can be born with few or no usable capabilities, which is safe but
    opaque — an operator would otherwise see a new session id and no reason for anything
    that later refuses. The line is rendered from the child's own
    ``capability_visibility_state()`` (the existing #2285/#3378 read model, no new
    mechanism).

    ⚠️ This is the EXPLANATION's test, not the enforcement's. That surface re-resolves
    with the sid on every read and was green on the broken code #3561 fixed; the
    inheritance itself is measured by the denied tool's side effect not happening,
    above. The assertions here are on the presence of the denied capability's NAME and
    of the invoking session's sid — never on layout, wording or ordering.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM(_out_name())
    reg = _registry(tmp_path, scripted)

    invoker = await _invoker(reg, narrowing={"tool_deny": [_DENIED_TOOL]})
    child_sid, replies = await _open_child(reg, invoker)

    text = "\n".join(m.text for m in replies if m.text)
    assert child_sid in text, "the reply does not name the session it opened"
    assert _DENIED_TOOL in text, (
        "the reply does not name the capability the new session inherited a denial of, "
        f"so the operator has no stated reason for a later refusal (reply: {text!r})"
    )
    assert "inherit" in text.lower(), (
        f"the reply does not say the restriction was INHERITED, which is the one thing "
        f"that tells the operator where it came from (reply: {text!r})"
    )


@pytest.mark.asyncio
async def test_an_unnarrowed_invoker_reply_stays_free_of_inheritance_noise(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: with nothing to inherit, the ``/session new`` reply says nothing about
    inheritance.

    The negative half of the leg above, and the one that makes it non-vacuous: a reply
    that always carried the line would satisfy the positive assertion while saying
    nothing about the child. It also pins the inert case — an operator who is not
    narrowed sees exactly what they saw before #3562.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM(_out_name())
    reg = _registry(tmp_path, scripted)

    invoker = await _invoker(reg, narrowing=None)
    child_sid, replies = await _open_child(reg, invoker)

    text = "\n".join(m.text for m in replies if m.text)
    assert child_sid in text, "the reply does not name the session it opened"
    assert "inherit" not in text.lower(), (
        f"the reply claims an inherited restriction where the invoker has none — the "
        f"positive leg would then be satisfied by a constant string (reply: {text!r})"
    )
