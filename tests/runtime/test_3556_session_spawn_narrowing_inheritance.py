"""Tier 2: OS invariant — a session spawned through the ``spawn_session`` TOOL is
born inside its spawner's per-session capability envelope, not inside whatever
envelope the spawner's LLM asked for.

#3556, the third site of the #3546 / #3553 fix-class and the one whose contract was
already WRITTEN DOWN. ``router_host_adapter.spawn_session`` forwarded its ``narrowing``
argument to ``spawn_session_recorded`` verbatim — and that argument is a ``spawn_session``
tool parameter, i.e. LLM-authored. The spawner's own sid-keyed #2103-S1a narrowing was
not a term, so a narrowed session widened itself by spawning a sibling and handing it the
task. The tool's parameter description tells the model the opposite, in both languages:
*"Optional per-session capability narrowing (restrict-only, cannot widen your envelope)"*
(``tools/descriptions/delegation.py``). This module measures whether that sentence is
true; before the fix it was not.

★ Reachability, measured BEFORE this test was designed, because the answer decides the
test's premises:

  * ``spawn_session`` IS in ``capability_profile._FLOORED_TOOLS["spawn"]``, so it is
    denied by BOTH the #1827-S4b untrusted-context floor and the #2081 ``_delegate``
    floor. A session tainted by untrusted external content therefore cannot reach this
    seam at all — "#1827's contextual narrowing is bypassable wholesale through
    ``spawn_session``" is FALSE, and this module does not claim it.
  * The #2103-S1a sid-keyed layer — the one this fix carries — has NO such default. A
    session narrowed with ``{"tool_deny": [...]}`` resolves
    ``tool_contextually_denied("spawn_session")`` to ``False``: the seam stays reachable
    unless the narrowing names ``spawn_session`` itself. That is the window the two legs
    below occupy, and ``test_narrowed_session_can_still_reach_the_session_spawn_seam``
    keeps the premise honest — if a future default DID floor ``spawn_session`` on this
    layer, that test goes red and tells the reader the legs' premises moved, rather than
    letting them pass for a reason that no longer exists.

The composition is the one #3553 derived (``capability_profile.
compose_narrowing_mappings``): **deny keys union, allow keys intersect, an ABSENT allow
key is ⊤**. It is reused rather than re-derived because ``narrowing`` sits here exactly
where an ``agent`` step's ``capabilities`` sits there — an argument the spawner imposes
on the child. The two legs are the two rules, one each, and are separable on purpose:

  * ``test_spawner_deny_survives_an_llm_requested_allow_list`` — the spawner is narrowed
    with ``tool_deny`` ONLY and its LLM requests ``narrowing={"tool_allow": [<the denied
    tool>]}``, i.e. asks for exactly what it was refused. Fails if the spawner's deny is
    not carried into the child.
  * ``test_spawner_allow_list_survives_an_llm_requested_narrowing`` — the spawner is
    narrowed with ``tool_allow`` ONLY and its LLM requests a ``tool_deny`` on an
    unrelated tool, which looks restrict-only and is the case the ⊤ rule decides: the
    child declares nothing on the allow axis, so the spawner's allow-list must survive
    verbatim rather than vanish.

Both witness a REAL side effect — the file ``write_file`` puts on disk — never a status
string, and ``write_file`` is a production, catalog-listed tool reached through a real
``RouterLoop`` turn on the real spawned session. (#3553 lost a whole test round to a
bespoke test-only tool: one that is not in the universal catalog is never advertised and
never dispatches, so both legs asserted an absence that was guaranteed for the wrong
reason.) ``test_the_witness_tool_actually_runs_when_permitted`` is the permanent positive
control for that failure mode, and every leg additionally asserts that the child's turn
actually reached its terminal LLM call before reading the disk — an absence observed
because nothing ran would otherwise be indistinguishable from an absence observed
because the capability was denied.

The completeness half of the gate lives in
``tests/runtime/test_3546_pipeline_driver_narrowing_inheritance.py``; this module is what its
``_SITE_PARENT_LAYERS`` entry for ``router_host_adapter.spawn_session`` points at, which
is what turns that entry from an intent record into a behaviour record.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import PresentationWiring
from reyn.runtime.spawn_routing import AuditOnlyNoSurface
from reyn.security.permissions.effective import tool_contextually_denied
from reyn.tools.session_spawn import _handle as _handle_session_spawn
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.permissions import make_resolver

#: The capability under test: a real, catalog-listed, side-effecting tool whose effect
#: is observable on disk without reading any return value.
_WITNESS_TOOL = "write_file"
#: A second real tool, used as the spawner's ``tool_allow`` content in leg 2 so the
#: allow-list is non-empty and demonstrably does NOT contain ``_WITNESS_TOOL``.
_OTHER_TOOL = "read_file"
#: The spawn seam itself — a spawner narrowed by an allow-list must keep it, or the
#: leg would be measuring "the spawner could not spawn" instead.
_SPAWN_TOOL = "spawn_session"
_OUT_NAME = "p3556_out.txt"

#: What the spawner's LLM asks the child to do. The child's scripted LLM keys off this
#: marker in its FIRST user-role message, which is the request forwarded through
#: ``submit_agent_request`` — the spawner's own first user message never contains it.
_CHILD_TASK = "p3556-child: write the witness file"


class _WritesOnceLLM:
    """A real ``_llm_caller``-shaped callable (the RouterLoop Tier-2 test seam) shared by
    every session in the run: the spawned child's first turn asks for ``write_file``,
    every other turn answers in plain text so each loop terminates whether or not the
    call was allowed.

    The role is decided from the session's OWN first user-role message, so the two
    sessions script themselves rather than being assigned by construction order. Not a
    ``MagicMock``: a drift in the ``call_llm_tools`` keyword contract raises ``TypeError``
    here exactly as it would in production. The LLM's content is incidental — what is
    under test is whether the OS lets the requested capability reach its handler.
    """

    def __init__(self) -> None:
        #: Turns taken by the SPAWNED child — the barrier every leg waits on.
        self.child_turns = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        messages = kwargs.get("messages") or []
        first_user = next(
            (m for m in messages if isinstance(m, dict) and m.get("role") == "user"), None,
        )
        text = str((first_user or {}).get("content") or "")
        if _CHILD_TASK not in text:
            return LLMToolCallResult(
                content="ok", tool_calls=[], finish_reason="stop", usage=TokenUsage(),
            )
        self.child_turns += 1
        if self.child_turns == 1:
            return LLMToolCallResult(
                content=None,
                tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {
                        "name": _WITNESS_TOOL,
                        "arguments": '{"path": "%s", "content": "child-ran"}' % _OUT_NAME,
                    },
                }],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            )
        return LLMToolCallResult(
            content="done", tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _registry(tmp_path: Path, scripted: "_WritesOnceLLM") -> AgentRegistry:
    """Real ``AgentRegistry`` + real ``Session`` factory + real ``RouterLoop`` + real
    ``PermissionResolver`` (with ``file.write`` approved, so a denial observed below is
    the capability narrowing and not the write gate)."""
    if not (tmp_path / "reyn.yaml").exists():
        (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
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
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


async def _narrowed_spawner(reg: AgentRegistry, narrowing: "dict | None") -> "tuple[Session, str]":
    """Spawn a REAL session under ``narrowing`` through the production spawn seam
    (``spawn_session_recorded``), so its sid-keyed ``config.yaml`` is written by
    production code — that file is the layer the fix reads back."""
    routing = AuditOnlyNoSurface()
    sid = await reg.spawn_session_recorded(
        "worker", mode="persistent", narrowing=narrowing,
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    session = reg.get_session("worker", sid)
    assert session is not None
    return session, sid


def _spawn_ctx(spawner: Session, reg: AgentRegistry, state_log: StateLog) -> ToolContext:
    """A ``ToolContext`` wired the way ``RouterLoop`` wires one for this tool:
    ``spawn_session_fn`` is the spawner's own ``RouterHostAdapter.spawn_session`` with
    ``chain_id`` pre-bound (``router_loop._spawn_session_bound_impl``). The tool handler
    and the adapter method — the site under test — are both the production objects."""
    host = spawner.router_host

    async def _spawn_session_bound(
        *, request: str, mode: str, narrowing: "dict | None" = None,
        base_dir: "str | None" = None,
        agent: "str | None" = None, session: "str | None" = None,
    ) -> dict:
        return await host.spawn_session(
            request=request, mode=mode, narrowing=narrowing,
            base_dir=base_dir, chain_id="p3556-chain",
            agent=agent, session=session,
        )

    return ToolContext(
        events=host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            agent_registry=reg, host=host, spawn_session_fn=_spawn_session_bound,
        ),
        state_log=state_log,
    )


async def _spawn_via_tool(
    spawner: Session, reg: AgentRegistry, state_log: StateLog,
    scripted: "_WritesOnceLLM", *, narrowing: "dict | None",
) -> dict:
    """Drive the REAL ``spawn_session`` handler with the ``narrowing`` an LLM would have
    written, then wait for the spawned child's turn to REACH ITS END (two scripted
    calls: the tool-call turn and the terminal one).

    The barrier is asserted, not just awaited: ``spawn_session`` is async-dispatch
    posture (it returns a spawn-ack while the child runs), so reading the disk without
    it would let "the child never ran" masquerade as "the capability was denied".
    """
    result = await _handle_session_spawn(
        {"request": _CHILD_TASK, "mode": "persistent", "narrowing": narrowing},
        _spawn_ctx(spawner, reg, state_log),
    )
    assert result.get("status") == "spawned", f"the spawn itself failed: {result!r}"
    # #3748: unbounded (owner policy, docs/deep-dives/contributing/
    # testing.md § Time). Waiting for the spawned child to finish a turn --
    # any absence observed on disk below would otherwise be meaningless.
    # No terminating assert here: the loop condition IS that check, and an
    # assert restating it can never fire (the loop only exits once it is
    # already true) -- a hang here surfaces via the kill stack showing this
    # exact `while`, which is the honest failure record.
    while scripted.child_turns < 2:
        await asyncio.sleep(0.05)
    return result


def _written(tmp_path: Path) -> "list[str]":
    """Every on-disk copy of the witness file, wherever the child's workspace resolved
    it — the side effect itself, not a status string."""
    return sorted(str(p) for p in tmp_path.rglob(_OUT_NAME))


# ── reachability premise ──────────────────────────────────────────────────────


def test_narrowed_session_can_still_reach_the_session_spawn_seam(tmp_path: Path) -> None:
    """Tier 2: the premise the two legs stand on — a session narrowed on the #2103-S1a
    layer is NOT thereby denied ``spawn_session``, so the seam is reachable from inside
    a narrowed envelope.

    ``spawn_session`` is floored by the #1827 untrusted-context profile and the #2081
    ``_delegate`` profile, which is why this module makes no claim about those layers.
    The sid-keyed layer is different: nothing denies the seam by default there. If that
    ever changes, this test fails and says so, instead of the legs below quietly
    becoming "the spawner could not spawn".
    """
    reg = AgentRegistry(project_root=tmp_path, session_factory=lambda profile, **kw: None)
    d = reg._session_state_dir("worker", "s1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(f"name: s\ntool_deny: [{_WITNESS_TOOL}]\n", encoding="utf-8")
    contextual, _ = reg.resolved_profile_for("worker", sid="s1")

    assert tool_contextually_denied(contextual, _WITNESS_TOOL)
    assert not tool_contextually_denied(contextual, _SPAWN_TOOL), (
        "a per-session narrowing now denies spawn_session by default — the two "
        "acceptance legs below would stop measuring inheritance and start measuring "
        "an entrance denial"
    )


def test_main_session_narrowing_is_readable_without_a_sid(tmp_path: Path) -> None:
    """Tier 2: ``per_session_narrowing`` reads the MAIN session's narrowing when the
    sid is ``None``.

    ``RouterHostAdapter.live_session_id`` is ``str | None`` and is ``None`` for a
    spawner that is its agent's main session, whose own narrowing lives at the
    agent-level state dir. Reading ``None`` as "no sid, therefore nothing to inherit"
    would silently exempt exactly that spawner from the composition — the same
    absent-key-read-as-empty mistake the ⊤ rule exists to avoid, one layer up.
    """
    reg = AgentRegistry(project_root=tmp_path, session_factory=lambda profile, **kw: None)
    d = reg._session_state_dir("worker", "main")
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(f"name: s\ntool_deny: [{_WITNESS_TOOL}]\n", encoding="utf-8")

    assert reg.per_session_narrowing("worker", None) == {"tool_deny": [_WITNESS_TOOL]}
    assert reg.per_session_narrowing("worker", None) == reg.per_session_narrowing(
        "worker", "main",
    )


# ── instrument check ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_witness_tool_actually_runs_when_permitted(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: instrument check — with the spawner narrowed only on an UNRELATED tool,
    the spawned child's ``write_file`` call really does reach its handler and put a file
    on disk.

    Without this, both acceptance legs would pass for the wrong reason if the tool call
    stopped being dispatchable at all (which is exactly what happened to an earlier draft
    of the #3553 sibling, on a non-catalog tool). The legs assert an absence; this asserts
    the presence they are the absence of.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM()
    reg = _registry(tmp_path, scripted)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    spawner, _sid = await _narrowed_spawner(reg, {"tool_deny": ["some_unrelated_tool"]})
    await _spawn_via_tool(spawner, reg, state_log, scripted, narrowing=None)

    assert _written(tmp_path), (
        "the spawned child's write_file never executed even with nothing narrowing it "
        "— the two acceptance legs below would be vacuous"
    )


# ── leg 1: the spawner's deny survives an LLM-requested allow-list ────────────


@pytest.mark.asyncio
async def test_spawner_deny_survives_an_llm_requested_allow_list(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a session cannot re-grant itself a denied tool by spawning a sibling and
    naming that tool in the ``spawn_session`` ``narrowing``.

    The requested ``tool_allow`` is restrict-shaped in isolation — on its own it would
    make the tool the child's ONLY reachable capability — so the seam had no reason to
    reject it; the spawner's ``tool_deny`` has to be carried in for the answer to stay a
    denial. Before the fix it was not, and the tool's real side effect happened, which is
    what made the tool's own "cannot widen your envelope" description false.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM()
    reg = _registry(tmp_path, scripted)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    spawner, _sid = await _narrowed_spawner(reg, {"tool_deny": [_WITNESS_TOOL]})
    ack = await _spawn_via_tool(
        spawner, reg, state_log, scripted, narrowing={"tool_allow": [_WITNESS_TOOL]},
    )

    assert not _written(tmp_path), (
        "a tool the SPAWNER's per-session narrowing denied executed its real side "
        f"effect in a session that spawner spawned for itself (ack: {ack!r})"
    )


# ── leg 2: the ⊤ rule — the spawner's allow-list survives ─────────────────────


@pytest.mark.asyncio
async def test_spawner_allow_list_survives_an_llm_requested_narrowing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a ``spawn_session`` ``narrowing`` that constrains only the DENY axis does
    not discard the spawner's own allow-list.

    This is the widening the composition's ⊤ rule closes: the requested narrowing says
    nothing about the allow axis, so the child's allow-list must be the spawner's
    (``parent ∩ ⊤ = parent``) rather than absent. Before the fix it was absent — an
    LLM could shed an allow-list restriction by writing a narrowing that looks stricter.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM()
    reg = _registry(tmp_path, scripted)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    spawner, _sid = await _narrowed_spawner(
        reg, {"tool_allow": [_OTHER_TOOL, _SPAWN_TOOL]},
    )
    ack = await _spawn_via_tool(
        spawner, reg, state_log, scripted,
        narrowing={"tool_deny": ["some_unrelated_tool"]},
    )

    assert not _written(tmp_path), (
        "a tool OUTSIDE the spawner's tool_allow executed its real side effect in a "
        f"session that spawner spawned, having asked only for a deny (ack: {ack!r})"
    )
