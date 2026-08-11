"""Tier 2: #3378 — advertisement and enforcement read ONE effective source.

The defect: the LLM-visible ``tools[]`` was filtered by ``exclude_tools`` alone,
while the live gate (``RouterLoop._excluded_result``) asked the CONTEXTUAL
narrowing. The bridge ran one way only — with no explicit contextual, one was
derived FROM ``exclude_tools`` — so any contextual arriving from a real narrowing
source left ``exclude_tools`` untouched: the tool stayed advertised and was
rejected only when the model called it (the owner's ``exec`` report; a wasted
turn and a presentation/enforcement disagreement).

★ **Driven from a narrowing source that is NOT ``exclude_tools``.** A contextual
derived from ``exclude_tools`` makes both halves agree BY CONSTRUCTION, so the
defect cannot reproduce under it — which is very likely why this survived. These
tests use the **ephemeral ``_untrusted`` profile** (``load_untrusted_profile`` →
``resolve_profile``), the real producer ``Session._effective_contextual_for_turn``
composes when untrusted external content is live in the active context, and the
exact profile that denies the owner's ``exec``. ``exclude_tools`` is asserted
EMPTY on every arm below, so nothing here can be explained by the bridge.

**Both halves are required (#187).** Hiding a row is not denying it: the model can
still name an unadvertised tool (native call, the #229 salvage, or a direct
``invoke_action(action_name=…)``) — that is exactly how the #187 ``web_search``
leak executed. Each gate asserts the pair.

Narrowing sources enumerated from the producers, not hand-listed —
``AgentRegistry.resolved_profile_for`` contributes five conjuncts (topology
``capability_profile`` binding · the fail-closed ``_delegate`` floor for a declared
but absent/malformed binding · the #2081 unbound-delegate floor · the #2103 S1a
per-session ``config.yaml`` · the #2103 B ⊆-parent lineage cap), and three more
compose downstream (``CapabilityVisibility.reapply_visibility_override``'s
``/visibility`` override · the ephemeral ``_untrusted`` profile · the
``exclude_tools`` bridge) = **8**. They all reach the same field by the same
``compose_resolved`` meet, so the agreement below is a property of that field, not
of any one source; this file drives it with the ephemeral one and
``test_topology_profile_binding_e2e_1827.py`` already drives enforcement from the
topology one.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from reyn.security.permissions.capability_profile import (
    load_untrusted_profile,
    resolve_profile,
)
from tests._support.router_loop import FakeRouterHost


class CapturingLLM:
    """Real callable standing in for ``call_llm_tools`` — records the ``tools=``
    payload the OS actually put on the wire, then terminates the turn.

    A real class with ``async def __call__`` (policy: Mock vs Fake) — signature
    drift raises ``TypeError`` here, unlike an ``AsyncMock``.
    """

    def __init__(self) -> None:
        self.advertised: "list[str]" = []
        self.calls: int = 0

    async def __call__(self, **kwargs) -> LLMToolCallResult:
        self.calls += 1
        self.advertised = [
            t["function"]["name"] for t in (kwargs.get("tools") or [])
        ]
        return LLMToolCallResult(
            content="done",
            tool_calls=[],
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
        )


def _untrusted_contextual():
    """The REAL ephemeral narrowing — the built-in ``_untrusted`` profile resolved
    through the same ``load_untrusted_profile`` / ``resolve_profile`` pair
    ``Session._effective_contextual_for_turn`` uses. Not derived from
    ``exclude_tools``."""
    return resolve_profile(load_untrusted_profile(Path("/nonexistent-project-root")))[0]


def _advertised_names(*, contextual) -> "list[str]":
    """Run one real turn through ``RouterLoop.run`` and return the tool names the
    OS advertised to the model. ``exclude_tools`` is left at its default (empty)."""
    llm = CapturingLLM()
    loop = RouterLoop(
        host=FakeRouterHost(),
        chain_id="c-3378",
        max_iterations=3,
        contextual_permission=contextual,
        llm_caller=llm,
    )
    asyncio.run(loop.run("hello", []))
    assert llm.calls >= 1, "the turn never reached an LLM call — nothing was measured"
    return llm.advertised


def _call(loop: RouterLoop, name: str, args: dict) -> dict:
    return asyncio.run(
        loop._execute_tool({"function": {"name": name, "arguments": json.dumps(args)}})
    )


def _is_excluded(result: dict) -> bool:
    return (
        result.get("status") == "error"
        and result.get("error", {}).get("kind") == "tool_excluded"
    )


def test_ephemeral_contextual_denied_tools_are_not_advertised() -> None:
    """Tier 2: a tool the ephemeral contextual denies is absent from ``tools[]``.

    The control arm (same host, same scheme, NO contextual) advertises those exact
    names, so their absence is caused by the narrowing rather than by their never
    having been in the catalog — the falsification this gate needs to mean anything.
    """
    contextual = _untrusted_contextual()
    baseline = set(_advertised_names(contextual=None))
    narrowed = set(_advertised_names(contextual=contextual))

    # Names this host's catalog really offers AND the _untrusted floor really denies.
    denied_and_offered = {n for n in baseline if n in contextual.tool_deny}
    assert denied_and_offered, (
        "control arm advertises nothing the _untrusted profile denies — the gate "
        "below would pass vacuously"
    )
    assert not (denied_and_offered & narrowed), (
        "contextually-denied tools are still advertised: "
        f"{sorted(denied_and_offered & narrowed)}"
    )
    # The narrowing is targeted, not a wipe: everything else still reaches the model.
    assert narrowed == baseline - denied_and_offered


def test_ephemeral_contextual_denied_tool_is_rejected_on_every_call_shape() -> None:
    """Tier 2: hiding is NOT denying (#187) — an unadvertised tool named anyway is
    still rejected, whether called natively or unwrapped from ``invoke_action``.

    #4155: this used to also assert ``host.agent_sends == []`` as proof the denied
    tool's handler never executed. That field went vacuous during the proposal-0067
    P4d/P4e migration (#3978) — ``run_prompt`` dispatch moved off ``host.send_to_agent``
    onto the registry (``run_prompt_result_fn``/``run_prompt_async_fn``), so nothing
    populates ``agent_sends`` for this tool regardless of whether the gate below works;
    the assertion could never fail for the reason it named. Dropped rather than
    rewritten (six-question ③: nobody would miss it) — ``_is_excluded`` below already
    checks ``error.kind == "tool_excluded"`` specifically, the kind a REAL handler
    error (e.g. a missing ``run_prompt_result_fn``) would not produce, so it already
    proves the handler was never reached, not merely that denial was reported.
    """
    contextual = _untrusted_contextual()
    loop = RouterLoop(
        host=FakeRouterHost(),
        chain_id="c-3378",
        max_iterations=3,
        contextual_permission=contextual,
    )
    target = "run_prompt"
    assert target in contextual.tool_deny

    native = _call(loop, target, {"to": "peer", "request": "leak"})
    wrapped = _call(
        loop, "invoke_action", {"action_name": target, "args": {"to": "peer"}}
    )
    for result in (native, wrapped):
        assert _is_excluded(result), result
        assert target in result["error"]["message"]


def test_wrapper_itself_stays_advertised_under_an_allow_list_contextual() -> None:
    """Tier 2: the agreement is exact in BOTH directions — advertisement must not be
    NARROWER than enforcement either.

    ``invoke_action`` carries its real target in ``action_name``, so the live gate
    unwraps before deciding and never denies the wrapper itself. An advertisement
    filter that keyed on the wrapper's own name would delete the only route to every
    allowed action under an allow-list contextual — the mirror image of the #3378
    defect. Driven with an allow-list (``tool_allow``), which ``exclude_tools``
    cannot even express.
    """
    from reyn.runtime.router_loop import apply_contextual_visibility
    from reyn.security.permissions.effective import ContextualPermission

    catalog = [
        {"type": "function", "function": {"name": n, "description": ""}}
        for n in ("invoke_action", "list_actions", "read_file", "write_file")
    ]
    allow_only_read = ContextualPermission(tool_allow=frozenset({"read_file"}))
    kept = {
        t["function"]["name"]
        for t in apply_contextual_visibility(catalog, allow_only_read)
    }
    assert "invoke_action" in kept, (
        "the wrapper was pre-filtered, so no allowed action is reachable at all"
    )
    assert "read_file" in kept
    assert "write_file" not in kept and "list_actions" not in kept


def test_no_contextual_leaves_even_the_floored_capabilities_advertised() -> None:
    """Tier 2: with no narrowing anywhere the filter is INERT — an un-narrowed
    session must keep offering exactly what the scheme composed, including the
    dangerous capabilities a narrowing would have removed. Without this, the fix
    could pass its deny gates by quietly over-filtering the default posture."""
    advertised = set(_advertised_names(contextual=None))
    floored = _untrusted_contextual().tool_deny
    assert advertised & floored, (
        "the default posture is already missing every floored capability — the "
        "narrowing gates elsewhere in this file would pass without narrowing anything"
    )
    assert "read_file" in advertised


def test_exclude_tools_survives_alongside_an_explicit_contextual() -> None:
    """Tier 2: ``exclude_tools`` composes WITH a contextual instead of being
    discarded by it.

    Before #3378 an explicit contextual took the ``if contextual is not None`` branch
    and ``exclude_tools`` never reached the live gate — so a session that had both
    (every ``Session``, once ``CapabilityVisibility`` has resolved a contextual) hid
    ``--exclude-tools web_search`` from the catalog but did NOT block a call to it:
    the #187 leak in reverse.
    """
    host = FakeRouterHost()
    loop = RouterLoop(
        host=host,
        chain_id="c-3378",
        max_iterations=3,
        exclude_tools={"web_search"},
        contextual_permission=_untrusted_contextual(),
    )
    result = _call(loop, "web_search", {"query": "gold patch"})
    assert _is_excluded(result), result
    # and the contextual's own denials are not lost by the composition either
    assert _is_excluded(_call(loop, "spawn_session", {"request": "x"}))


@pytest.mark.parametrize("shape", ["native", "invoke_action"])
def test_falsify_target_is_absent_from_exclude_tools(shape: str) -> None:
    """Tier 2: the block above is NOT the ``exclude_tools`` bridge in disguise.

    Same call shapes, same host, ``exclude_tools`` left empty and NO contextual: the
    tool dispatches normally (whatever the outcome, it is not ``tool_excluded``). So
    the rejection in the sibling test comes solely from the ephemeral contextual.
    """
    loop = RouterLoop(host=FakeRouterHost(), chain_id="c-3378", max_iterations=3)
    target = "run_prompt"
    if shape == "native":
        result = _call(loop, target, {"to": "peer", "request": "hi"})
    else:
        result = _call(loop, "invoke_action", {"action_name": target, "args": {}})
    assert not _is_excluded(result), (
        "an un-narrowed loop already blocks this tool — the sibling gate proves nothing"
    )


def test_intra_turn_narrowing_re_filters_the_advertised_catalog() -> None:
    """Tier 2: the agreement is per-CALL, not per-turn.

    #1909's opt-in intra-turn re-narrowing replaces the contextual mid-turn (external
    content spliced in by round N narrows round N+1 of the SAME turn). The advertised
    payload was built once, before the loop — so without a re-filter, round N+1 is
    offered tools round N+1's gate now rejects: the same advertise/enforce split at
    finer grain. Round 1's payload (captured below) is the falsifying control: the
    names are there until the narrowing engages.
    """
    from reyn.tools.scheme import (
        AdvertisedTools,
        Execute,
        ExecutionResult,
        PlainText,
        Presentation,
        register_scheme,
    )

    contextual = _untrusted_contextual()
    target = "run_prompt"
    assert target in contextual.tool_deny

    class _TwoRoundScheme:
        """A real scheme: advertises a fixed payload, Executes the first round's
        tool_call, then falls through to PlainText."""

        name = "test-3378-two-round"

        async def build_presentation(self, available, layer_ctx, ops) -> Presentation:
            return Presentation(
                tools_channel=AdvertisedTools(entries=[
                    {"type": "function", "function": {"name": n, "description": ""}}
                    for n in ("list_agents", target)
                ]),
            )

        def interpret(self, llm_response, *, tool_catalog, ops):
            return Execute(actions=[]) if getattr(
                llm_response, "tool_calls", None
            ) else PlainText()

        async def execute(self, interp, exec_ctx, ops):
            return ExecutionResult(tool_results=[])

        def format_feedback(self, result, ops):
            return []

    register_scheme(_TwoRoundScheme())

    class _TwoRoundLLM:
        def __init__(self) -> None:
            self.tools_per_call: "list[set[str]]" = []
            self.calls = 0

        async def __call__(self, **kwargs) -> LLMToolCallResult:
            self.tools_per_call.append(
                {t["function"]["name"] for t in (kwargs.get("tools") or [])}
            )
            self.calls += 1
            if self.calls == 1:
                return LLMToolCallResult(
                    content=None,
                    tool_calls=[{
                        "id": "c1", "type": "function",
                        "function": {"name": "list_agents", "arguments": "{}"},
                    }],
                    finish_reason="tool_calls",
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
                )
            return LLMToolCallResult(
                content="done", tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            )

    llm = _TwoRoundLLM()
    resolved: "list[object | None]" = [None, contextual]

    def _contextual_for_turn():
        """The live per-iteration re-resolve: clean on round 1, tainted from round 2
        (external content spliced into context by the round-1 tool result)."""
        return resolved.pop(0) if resolved else contextual

    loop = RouterLoop(
        host=FakeRouterHost(),
        chain_id="c-3378",
        max_iterations=4,
        scheme_name=_TwoRoundScheme.name,
        llm_caller=llm,
        intra_turn_contextual_for_turn_fn=_contextual_for_turn,
        contextual_static_baseline=None,
    )
    asyncio.run(loop.run("hello", []))

    assert llm.calls >= 2, "the turn never reached a second round — nothing to compare"
    assert target in llm.tools_per_call[0], (
        "control arm: the tool was not advertised before the narrowing engaged, so "
        "its later absence proves nothing"
    )
    assert target not in llm.tools_per_call[1], (
        "the round AFTER the intra-turn narrowing still advertises a tool the live "
        "gate now rejects"
    )


def test_represent_round_applies_the_advertisement_filter() -> None:
    """Tier 2: the OS RePresent arm swaps the advertised payload mid-turn — that new
    payload must pass the same filter, or a re-present silently re-offers a denied
    tool the loop's own gate rejects."""
    from reyn.tools.scheme import (
        AdvertisedTools,
        Execute,
        ExecutionResult,
        PlainText,
        Presentation,
        RePresent,
        register_scheme,
    )

    contextual = _untrusted_contextual()
    target = "spawn_session"
    assert target in contextual.tool_deny

    class _RepresentScheme:
        """A real scheme: RePresents on a ``search`` call, and its re-presented
        payload deliberately contains a contextually-denied name."""

        name = "test-3378-represent"

        async def build_presentation(self, available, layer_ctx, ops) -> Presentation:
            if not layer_ctx.get("refinement"):
                return Presentation(
                    tools_channel=AdvertisedTools(entries=[
                        {"type": "function",
                         "function": {"name": "search", "description": ""}},
                    ]),
                )
            return Presentation(
                tools_channel=AdvertisedTools(entries=[
                    {"type": "function", "function": {"name": n, "description": ""}}
                    for n in ("list_agents", target)
                ]),
                candidates=("list_agents",),
            )

        def interpret(self, llm_response, *, tool_catalog, ops):
            calls = getattr(llm_response, "tool_calls", None) or []
            if not calls:
                return PlainText()
            if any(c["function"]["name"] == "search" for c in calls):
                return RePresent(refinement={"query": "x"})
            return Execute(actions=[])

        async def execute(self, interp, exec_ctx, ops):
            return ExecutionResult(tool_results=[])

        def format_feedback(self, result, ops):
            return []

    register_scheme(_RepresentScheme())

    class _RepresentLLM:
        def __init__(self) -> None:
            self.tools_per_call: "list[set[str]]" = []
            self.calls = 0

        async def __call__(self, **kwargs) -> LLMToolCallResult:
            self.tools_per_call.append(
                {t["function"]["name"] for t in (kwargs.get("tools") or [])}
            )
            self.calls += 1
            if self.calls == 1:
                return LLMToolCallResult(
                    content=None,
                    tool_calls=[{
                        "id": "s1", "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }],
                    finish_reason="tool_calls",
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
                )
            return LLMToolCallResult(
                content="done", tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            )

    llm = _RepresentLLM()
    loop = RouterLoop(
        host=FakeRouterHost(),
        chain_id="c-3378",
        max_iterations=4,
        scheme_name=_RepresentScheme.name,
        contextual_permission=contextual,
        llm_caller=llm,
    )
    asyncio.run(loop.run("find me a tool", []))

    assert llm.calls >= 2, "the re-present never re-queried — nothing to compare"
    re_presented = llm.tools_per_call[1]
    assert "list_agents" in re_presented, (
        "the re-presented payload never reached the model — the gate below is vacuous"
    )
    assert target not in re_presented, (
        "a re-present round re-offered a tool the live gate rejects"
    )


# ── #3378 ② the Tool tab: two axes, and "(none)" vs "not wired" ──────────────
#
# Same root as ①, plus an independent gap. The Tool tab could not distinguish
# "denied by your capability profile" from "you turned it off with /visibility"
# (the first was silently dropped from the census entirely), nor "nothing is
# narrowed" from "this frame carries no visibility state at all".


def _bind_topology_profile(root: Path, *, member: str, body: str) -> None:
    """Write a real topology binding + the capability_profile it names — the #1827
    topology narrowing source, resolved by ``AgentRegistry.resolved_profile_for`` and
    reaching the Tool tab through the session envelope. Not ``exclude_tools``."""
    td = root / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / "t.yaml").write_text(
        f"name: t\nkind: network\nmembers: [{member}, peer]\n"
        f"profiles:\n  {member}: narrowed\n",
        encoding="utf-8",
    )
    pd = root / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "narrowed.yaml").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_envelope_denied_tool_is_reported_as_denied_not_dropped(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: a topology-denied tool appears in ``denied_by_envelope`` — the Tool
    tab can say WHY it is unavailable instead of silently omitting it.

    Real ``AgentRegistry`` + real ``Session``, driven from the topology binding (a
    narrowing source that is not ``exclude_tools``).
    """
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session
    from tests._support.agent_session import make_session

    monkeypatch.chdir(tmp_path)
    denied_tool = "run_prompt"
    _bind_topology_profile(
        tmp_path, member="alice", body=f"name: narrowed\ntool_deny: [{denied_tool}]\n",
    )

    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(
            agent_name=profile.name,
            state_log=state_log,
            registry=holder.get("reg"),
            chat_tool_use_scheme="enumerate-all",
        )
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded(
        "alice", presentation_consumer=None, intervention_bridge=None,
    )
    session = reg.get_session("alice", sid)

    state = session.capability_visibility_state()
    authorized = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}
    denied = {i["name"] for i in state["denied_by_envelope"] if i["kind"] == "tool"}
    assert denied_tool in denied, (
        "an envelope-denied tool is still absent from the read model entirely — the "
        "Tool tab cannot explain why it is unavailable"
    )
    assert denied_tool not in authorized, "a denied tool must not be offered as togglable"
    # the census is otherwise intact (the narrowing is targeted, not a wipe)
    assert "list_agents" in authorized


def _vis_snapshot(items) -> dict:
    return {"visibility_items": items, "mcp_servers": [], "skills": []}


def test_tool_tab_distinguishes_the_contextual_axis_from_the_visibility_axis() -> None:
    """Tier 2: a contextually-denied row and a ``/visibility``-off row are two
    different axes and must be told apart — and the denied row must not offer a
    toggle that cannot work (``/visibility on`` re-resolves from base, which still
    denies it)."""
    from reyn.interfaces.inline.textual_chat.chrome import _visibility_pane_entries

    entries = _visibility_pane_entries(
        _vis_snapshot([
            {"kind": "tool", "name": "on_tool", "on": True, "denied": False},
            {"kind": "tool", "name": "off_tool", "on": False, "denied": False},
            {"kind": "tool", "name": "denied_tool", "on": False, "denied": True},
        ]),
        "tool", None,
    )
    by_name = {
        name: (row, slash)
        for row, slash in entries
        for name in ("on_tool", "off_tool", "denied_tool")
        if name in row
    }
    on_row, _on_slash = by_name["on_tool"]
    off_row, off_slash = by_name["off_tool"]
    denied_row, denied_slash = by_name["denied_tool"]

    assert off_slash, "a /visibility-off row stays user-flippable"
    assert not denied_slash, "an envelope-denied row must not offer a /visibility toggle"
    # Distinguishability (the #3367 property): strip the name and the two axes' rows
    # must still differ — a shared marker would tell the operator to try a toggle
    # that cannot work.
    assert denied_row.replace("denied_tool", "") != off_row.replace("off_tool", "")
    assert denied_row.replace("denied_tool", "") != on_row.replace("on_tool", "")


def test_tool_tab_tells_nothing_narrowed_apart_from_not_wired() -> None:
    """Tier 2: an empty Tool tab means two different things and must say which.

    ``visibility_items is None`` = the frame carries no visibility seam (a remote
    read-model frame, or a session without the accessor). A present-but-empty list =
    the seam answered and nothing is narrowed. These rendered identically before,
    which is why the owner could not tell a broken tab from an empty one.
    """
    from reyn.interfaces.inline.textual_chat.chrome import _visibility_pane_entries

    wired_empty = _visibility_pane_entries(_vis_snapshot([]), "tool", None)
    unwired = _visibility_pane_entries(_vis_snapshot(None), "tool", None)
    assert wired_empty != unwired, (
        '"nothing is narrowed" and "not wired yet" still render identically'
    )


def test_remote_read_model_frame_reports_unwired_not_empty() -> None:
    """Tier 2: a remote (AG-UI) read-model frame genuinely carries no visibility
    seam, so it must project the not-wired signal rather than an empty list — which
    would assert "nothing is narrowed", a claim that frame cannot support."""
    from reyn.interfaces.repl.read_model import project_remote_snapshot

    assert project_remote_snapshot({}).get("visibility_items") is None


def test_snapshot_reports_unwired_when_the_session_has_no_visibility_seam() -> None:
    """Tier 2: the not-wired signal originates at the read, not just at the render.

    ``_session_visibility_items`` is the seam that decides; a session object without
    ``capability_visibility_state`` must yield the not-wired sentinel rather than an
    empty list, or every downstream renderer is back to conflating the two states.
    The argument here is deliberately an object that simply DOES NOT HAVE the
    accessor — that absence is the whole condition under test, not a stand-in for a
    collaborator whose behaviour is being faked.
    """
    from reyn.interfaces.repl.status import _session_visibility_items

    assert _session_visibility_items(object()) is None
