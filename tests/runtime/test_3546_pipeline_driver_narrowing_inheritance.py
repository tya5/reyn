"""Tier 2: OS invariant — a pipeline driver-session is born with the invoker's
per-session capability narrowing, and every spawn call site declares one — and
(#3553) declares which of its spawner's envelope layers that narrowing composes.

#3546. ``AgentRegistry.spawn_session_recorded`` is the single seam at which a new
permission envelope is BORN. Two of its three call sites pass ``narrowing=``
(``router_host_adapter``'s ``session_spawn``, ``session_api``'s ``agent`` step);
the pipeline driver-session spawn (``session_api._spawn_pipeline_driver_session``)
did not — so the driver-session that actually runs a pipeline's steps was
constructed WITHOUT the invoker's per-session narrowing, whatever any downstream
dispatch does or does not re-check.

The inheritance is counted HERE, at the place that should inherit, not at the
places that might have re-checked: call seams are unbounded (a new dispatch path
can be added at any time), whereas the sites where an envelope is born are
bounded and AST-enumerable. That is what ``test_every_spawn_site_passes_narrowing``
pins.

#3553 widened that gate twice, because passing SOME ``narrowing=`` value turned out
to be satisfiable by a value that is not a function of the parent at all — which is
what the ``agent`` step's own spawn was doing one level down, green here the whole
time. The enumeration now also covers ``spawn_ephemeral_session`` (the forwarder,
so the site that DECIDES the agent-step value is inside the gate rather than one
hop outside it), and every enumerated site must declare in ``_SITE_PARENT_LAYERS``
which layers of its spawner's envelope its value composes, plus the behavioural
test that measures the claim. ⚠️ That declaration is an INTENT record: a site whose
prose and code disagree stays green. It is the index of the per-site behavioural
tests, not a substitute for them — see ``_SiteDeclaration``. Enumerating the sites
also surfaced a THIRD member of this fix-class (the ``session_spawn`` tool's spawn
passed its LLM's requested narrowing, never its spawner's), declared as such and
filed as #3556 rather than fixed on a guess; #3556 then composed the spawner's layer
in and replaced that site's ``unmeasured_reason`` with the two behavioural legs in
``tests/runtime/test_3556_session_spawn_narrowing_inheritance.py``.

#3561 widened it a third time, on the axis the first two widenings had in common. Both
earlier gates decided membership by SHAPE — #3554's by whether a site spelled
``narrowing=`` (which #3556 satisfied while passing a value that was no function of its
parent), and the enumeration itself by whether a callee's NAME was on a list. Neither
question is the one that matters. ``AgentRegistry.spawn_session`` — the sync primitive
that ``spawn_session_recorded`` itself calls, and that four other sites call directly —
was outside the gate because it took no ``narrowing`` argument at all (it does since
#3562), i.e. it failed the shape check in the opposite direction. "It cannot inherit"
is not "it need not
inherit": an API with no inheritance channel is an unmet requirement. The criterion is
REACHABILITY — can a narrowed subject cause this to run — and it is now listed, with the
walk resolving calls by RECEIVER so ``spawn_session`` the registry primitive is not
conflated with the two other functions of that name (see ``_Seam``).

Reachability is measured, not assumed:
``tests/runtime/test_3561_spawn_session_seam_reachability.py`` drove an agent step whose prompt
was a previous agent step's MODEL OUTPUT, on a session narrowed to one capability, and
observed it reach ``/session new`` and spawn — ``Session._handle_user_message``
short-circuited to ``_maybe_handle_slash`` before the router turn, so the reaching turn
made no LLM call at all. The pre-measurement guess ("slash is operator-initiated, so it
is out of scope") was false.

★ #3595 step 1 then made it TRUE, by ruling that path a defect: the agent-step prompt
rides its own inbox kind instead of claiming ``kind="user"``, so it never enters the
``startswith('/')`` dispatch, and that file's leg is the same measurement INVERTED (the
absence of the spawn, plus an operator control proving the command still runs). The
enumeration criterion is unchanged and so is this site's membership — this list counts
every place a child envelope is BORN, not every place a model can reach. What #3595
retires is the severity argument, not the entry (#3596 / #3562-#3586).

★ Enumerating the primitive immediately paid for itself: measuring the CRASH-RECOVERY
sites (``restore_all`` / ``_rewake_pipeline_runs``), which reach ``spawn_session``
directly and had never been counted, found that a re-woken session was reborn OUTSIDE
its own persisted per-session narrowing — resolvable on the operator's status bar,
un-enforced in the RouterLoop. #3561 fixes that in ``spawn_session`` itself. That is the
concrete answer to "does a seam with no inheritance channel matter": it did, and the
shape check would never have asked.

Every enumerated site is accounted for today — a state this file records but does not
enforce, since a NEW site may register an ``unmeasured_reason`` and stay green here.

★ #3562 closed the one declared GAP and, in doing so, expired the exemption every
``spawn_session`` site rested on. ``/session new`` composes its invoker's #2103-S1a
layer in (uniformly — see its declaration for why there is no cross-identity branch),
which required the primitive to grow the ``narrowing`` channel #3561 had recorded as an
UNMET REQUIREMENT. ``test_no_exemption_claims_a_channel_that_exists`` is what made that
expiry mechanical rather than remembered: the four sites that still pass nothing are
re-argued on the merits — no spawner to inherit from (``resolve_session``); a value
would OVERWRITE the recovering session's own durable layer, since the primitive
persists what it is given (the recovery pair); and, for ``spawn_session_recorded``, the
injection happens at a LATER point than the primitive offers. ★ That last one was
measured, not judged: routing its ``narrowing`` through the new channel REDded two
existing behavioural tests, because ``refresh_config_projections()`` runs in between and
its ``reapply_visibility_override`` re-resolves-from-base-and-SETs — with no registry
back-reference there was no base, so it landed on ALLOW-ALL and discarded the injection.
"The channel exists, therefore use it" would have been a shape argument in the third
direction. ★ #3593 ① removed that discard (no base obtained ⇒ the live envelope is
PRESERVED, never overwritten with a fabricated one), and re-measured: under the fix both
of those tests stay GREEN with the value routed down the channel. The site keeps its
current ordering — #3593 ① is scoped to the fail-open write and deliberately does not
move a spawn seam's injection point — so the exemption below now rests on "not moved,
pending its own PR", not on "the refresh would eat it". The ``_S3561`` legs assert reachability only, never anything about what the
child inherits, so they are orthogonal to #3562's and stay green through it — verified
by running them (both the pre-#3595 leg and, after the rebase, its #3596 inverse plus
the operator control), not assumed.

Scope of what this fix carries (the layers a session's live capability envelope
is composed from — enumerated, not assumed):

  1. agent ``permissions`` declaration + topology ``capability_profile`` bindings
     + the #2081 ``_delegate`` floor — all keyed by the AGENT NAME, which the
     driver-session shares with its invoker, so ``resolved_profile_for`` re-derives
     them identically. NOT lost; this is the part the "⊆ by construction" prose
     was right about.
  2. the #2103-S1a per-session ``config.yaml`` narrowing — keyed by SID. A fresh
     sid has no ``config.yaml``, so this layer resolved to nothing on the driver.
     LOST; this is the layer the fix carries.
  3. the #2285 in-memory ``/visibility`` override — an operator view toggle on a
     live ``Session`` object, never persisted and never passed as ``narrowing=``
     at either sibling spawn site either. Out of scope here (same treatment as
     the siblings), noted so "inherited" is not read as "all layers inherited".
  4. the #1827-S4b ephemeral untrusted-context narrowing — NOT an inheritable
     value at all: ``Session._ephemeral_contextual_for_turn`` re-derives it every
     turn from THAT session's own live history plus the
     ``safety.threat_scan.capability_narrowing`` opt-in, so there is no state to
     pass through ``narrowing=`` (a dict). Whether a taint on the invoker's
     history should transfer to a driver-session it spawns is a separate question
     about taint propagation, not about this inheritance seam.

``test_narrowed_invoker_pipeline_tool_step`` is the reachability witness the
architect set as the acceptance condition: it drives the REAL ``run_pipeline``
tool handler from a REAL narrowed session and observes whether the denied tool's
own side effect happens. Measured on the unfixed code: the side effect HAPPENED,
so the gap is reachable, not latent.

That witness needs BOTH halves of the fix, which is itself a measurement: with
the spawn seam alone the driver-session carries the narrowing and the tool still
ran, because ``pipeline_verbs._make_tool_dispatch`` is the one tool-dispatch path
that executes outside a ``RouterLoop`` — so neither the RouterLoop advertisement
filter nor its ``_excluded_result`` call-time gate was in the path to read it.
Each half is strip-falsifiable on its own: removing ``narrowing=`` REDs this test
plus the two envelope tests below; removing the dispatch consumer REDs this test
alone.
"""
from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import spawn_ephemeral_session
from reyn.runtime.session_params import PresentationWiring
from reyn.runtime.spawn_routing import AuditOnlyNoSurface
from reyn.tools.pipeline_verbs import _handle_run_pipeline
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.paths import REPO_ROOT

_DENIED_TOOL = "p3546_denied_step"

_PIPELINE_DSL = f"""
pipeline: main
steps:
  - tool: {{name: {_DENIED_TOOL}, args: {{tag: step-ran}}, output: o0}}
"""


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real ``AgentRegistry`` + real ``Session`` factory (the harness shape
    ``tests/runtime/test_3093_pipeline_registry_spawn_propagation.py`` uses)."""
    if not (tmp_path / "reyn.yaml").exists():
        (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_wiring=PresentationWiring(
                presentation_consumer=presentation_consumer,
                intervention_bridge=intervention_bridge,
            ),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


def _install_side_effect_tool(monkeypatch, out_file: Path) -> None:
    """A REAL side-effecting tool: appends a line per call. Its file is the
    witness that the step's capability actually executed — a denial that only
    changed a status string would not prove the side effect was prevented."""
    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        p = Path(out_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(str(args.get("tag", "x")) + "\n")
        return {"tag": str(args.get("tag", "x"))}

    tool = ToolDefinition(
        name=_DENIED_TOOL,
        description="#3546 test: append a line per call (real side effect).",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow"),
        handler=_handler,
        category="io",
        purity="side_effect",
    )
    base = tools_pkg.get_default_registry

    def _with_tool():
        registry = base()
        registry.register(tool)
        return registry

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)


def _install_pipeline_to_disk(tmp_path: Path, *, key: str = "ns") -> None:
    """Production-shaped install: an on-disk DSL file declared via
    ``.reyn/config/pipelines.yaml`` — what a spawned session's config-projection
    refresh actually rebuilds from."""
    d = tmp_path / "pipelines"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ns.yaml").write_text(_PIPELINE_DSL, encoding="utf-8")
    cfg_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.dump({"pipelines": {"entries": {key: {"path": "pipelines/ns.yaml"}}}}),
        encoding="utf-8",
    )


async def _narrowed_invoker(reg: AgentRegistry) -> "tuple[Session, str]":
    """Spawn a REAL session narrowed so ``_DENIED_TOOL`` is denied, through the
    production spawn seam (``spawn_session_recorded`` — the same call the
    ``session_spawn`` tool makes), and return it with its sid.

    ``run_pipeline`` itself is deliberately NOT in the deny-set: the architect's
    acceptance condition needs the narrowed session to still be able to LAUNCH a
    pipeline, so that what the run does with the denied step is what the test
    observes.
    """
    routing = AuditOnlyNoSurface()
    sid = await reg.spawn_session_recorded(
        "worker", mode="persistent",
        narrowing={"tool_deny": [_DENIED_TOOL]},
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    session = reg.get_session("worker", sid)
    assert session is not None
    return session, sid


def _tool_ctx(session: Session, reg: AgentRegistry, state_log: StateLog) -> ToolContext:
    return ToolContext(
        events=session.router_host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=session.pipeline_registry,
            agent_registry=reg,
            host=session.router_host,
        ),
        state_log=state_log,
    )


def _spawned_narrowings(state_log: StateLog) -> "list[dict | None]":
    """The ``narrowing`` recorded on every ``session_spawned`` WAL entry — the
    durable, public audit surface ``spawn_session_recorded`` documents as
    "config-complete (mode + narrowing) for symmetric re-materialise"."""
    return [
        e.get("narrowing")
        for e in state_log.iter_from(0)
        if e.get("kind") == "session_spawned"
    ]


@pytest.mark.asyncio
async def test_narrowed_invoker_pipeline_tool_step(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the architect-set reachability witness for #3546 — a session
    narrowed to deny ``_DENIED_TOOL`` launches a pipeline whose only step invokes
    that tool, through the REAL ``run_pipeline`` handler. The tool's real side
    effect must not happen.

    This is the ONLY assertion that settles whether the missing inheritance is
    REACHABLE: everything upstream of it is a claim about the envelope, this is a
    claim about the capability.
    """
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_side_effect_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log)
    _install_pipeline_to_disk(tmp_path)

    invoker, _sid = await _narrowed_invoker(reg)
    await invoker._reapply_pipelines({})
    assert invoker.pipeline_registry.get("ns.main") is not None

    result = await _handle_run_pipeline(
        {"name": "ns.main", "input": None}, _tool_ctx(invoker, reg, state_log),
    )

    assert not out_file.exists(), (
        f"a tool denied by the invoker's per-session narrowing executed its real "
        f"side effect inside a pipeline tool step (run result: {result!r})"
    )


@pytest.mark.asyncio
async def test_pipeline_driver_session_inherits_invoker_narrowing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the driver-session spawned to run a pipeline is BORN carrying the
    invoker's per-session narrowing.

    Read off the ``session_spawned`` WAL entries (the durable audit surface the
    spawn seam documents as config-complete), so the assertion does not depend on
    the driver-session still being alive after its ephemeral teardown.
    """
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_side_effect_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log)
    _install_pipeline_to_disk(tmp_path)

    invoker, _sid = await _narrowed_invoker(reg)
    await invoker._reapply_pipelines({})
    before = _spawned_narrowings(state_log)
    assert before == [{"tool_deny": [_DENIED_TOOL]}]

    await _handle_run_pipeline(
        {"name": "ns.main", "input": None}, _tool_ctx(invoker, reg, state_log),
    )

    # Every session the launch spawned, and there must be at least the driver.
    born_by_the_launch = _spawned_narrowings(state_log)[len(before):]
    assert born_by_the_launch == [{"tool_deny": [_DENIED_TOOL]}], (
        "the pipeline launch's spawned session(s) were not born with the "
        f"invoker's per-session narrowing: {born_by_the_launch!r}"
    )


# ── the completeness gate: every place an envelope is BORN ────────────────────

#: ``(module path relative to src/, enclosing function name)`` for a spawn call
#: site that does NOT pass ``narrowing=``, with the reason. A new site either
#: passes ``narrowing=`` or is registered here with a reason a reviewer can weigh
#: — the #3484 ``*_UNMEASURED`` idiom.
#:
#: #3561 filled it, for one reason only: every entry called
#: ``AgentRegistry.spawn_session``, whose signature had no ``narrowing`` parameter.
#: That was deliberately recorded as an unmet requirement, not as a merit-based
#: exemption — "the API cannot take one" would be a shape argument, and this arc has
#: been slipped past on the shape axis twice already.
#:
#: ★ #3562 MET that requirement: the primitive now takes ``narrowing``, and the
#: exemption that rested on its absence expired exactly as
#: ``test_narrowing_exempt_sites_have_no_narrowing_channel`` was built to make it. One
#: entry left the table by USING the channel (``/session new`` composes its invoker's
#: layer in). The four that remain are re-argued ON THE MERITS below, individually —
#: two shapes: there is no SPAWNER whose narrowing could be inherited (and passing one
#: would not be a no-op but an overwrite of the child's own durable layer), or — for
#: the recorded seam — the enforcement must happen at a LATER point than the primitive
#: offers, which was measured, not assumed.
_EXEMPT_NO_SPAWNER_SESSION = (
    "no spawner SESSION exists at this seam (#3562): it is the inbound-transport "
    "get-or-spawn for a `<transport>:<native_id>` conversation key, entered from a "
    "transport frame, so there is no per-session layer to carry across. The child "
    "re-derives the agent's NAME-keyed layers itself. See this site's "
    "_SITE_PARENT_LAYERS entry."
)
_EXEMPT_RECOVERY_REENTRY = (
    "crash recovery RE-ENTERS an existing sid rather than spawning a child (#3562): "
    "that session's own #2103-S1a config.yaml is already on disk and is resolved + "
    "injected by the primitive itself (#3561). There is no spawner here, and passing a "
    "narrowing would not be a no-op — the primitive PERSISTS what it is given, so any "
    "value would overwrite the recovering session's own durable layer. See this site's "
    "_SITE_PARENT_LAYERS entry."
)
_EXEMPT_RECORDED_SEAM_INJECTS_LATER = (
    "the recorded seam writes + injects its OWN ``narrowing`` a few statements after "
    "the spawn. That ordering was MEASURED (#3562): it had to happen AFTER its "
    "``refresh_config_projections()``, whose ``reapply_visibility_override`` "
    "re-resolves the envelope from base and SETs it — on a session with no registry "
    "back-reference there was no base, so it set ALLOW-ALL and discarded anything "
    "injected earlier. Handing the value down the primitive's channel was tried and "
    "REDded tests/runtime/test_2103_s1bc_session_spawn_tool.py::"
    "test_spawn_session_recorded_enforces_narrowing_on_live_session and "
    "tests/runtime/test_pipeline_a2_spawn_ephemeral_session.py::"
    "test_spawn_ephemeral_session_narrowing_applied, both with an empty live tool_deny. "
    "#3593 (1) removed that discard — no base obtained now PRESERVES the live envelope "
    "instead of overwriting it — and re-measured: both tests stay GREEN with the value "
    "routed down the channel. The site is unchanged because #3593 (1) is scoped to the "
    "fail-open write, not because the refresh would still eat it; moving the injection "
    "is a behavioural change owed its own PR."
)
_NARROWING_EXEMPT_SITES: "dict[tuple[str, str], str]" = {
    ("reyn/runtime/registry.py", "spawn_session_recorded"): (
        _EXEMPT_RECORDED_SEAM_INJECTS_LATER
    ),
    ("reyn/runtime/registry.py", "resolve_session"): _EXEMPT_NO_SPAWNER_SESSION,
    ("reyn/runtime/registry.py", "restore_all"): _EXEMPT_RECOVERY_REENTRY,
    ("reyn/runtime/registry.py", "_rewake_pipeline_runs"): _EXEMPT_RECOVERY_REENTRY,
}

#: The exemption text #3561 used while the primitive had no ``narrowing`` parameter.
#: Kept as a named constant with no users so
#: ``test_no_exemption_claims_a_channel_that_exists`` can check that nothing re-adopts
#: it — the claim it makes is now false, and a false reason is worse than none.
_UNMET_NO_NARROWING_CHANNEL = (
    "calls AgentRegistry.spawn_session, which has no narrowing parameter (#3561): an "
    "UNMET REQUIREMENT recorded so the gate counts the site, not an exemption on the "
    "merits. See this site's _SITE_PARENT_LAYERS entry for what decides its child's "
    "envelope instead."
)

@dataclass(frozen=True)
class _Seam:
    """A function at which a child session's permission envelope is decided,
    named by its DEFINITION SITE rather than by its name (#3561).

    The #3553 version of this gate matched call sites by NAME alone, which is
    sound only while a name has exactly one definition in ``src/``. It does not
    for ``spawn_session``: ``AgentRegistry.spawn_session`` (the primitive),
    ``RouterHostAdapter.spawn_session`` (the ``session_spawn`` tool's adapter) and
    ``RouterLoop``'s host-protocol call all spell the same six characters, and a
    name match conflates them — it reports the ``router_loop.py`` host call as a
    site of the registry primitive, which it is not.

    ``receivers`` is how a call is RESOLVED: the key is
    ``(calling module, ast.unparse(receiver expression))`` — ``""`` for a bare-name
    call — and the value says whether that call is THIS seam. An unlisted receiver
    is an error, not a guess: the instrument refuses to attribute a call it cannot
    resolve, so a new call site with an unfamiliar receiver REDs
    ``test_spawn_seam_receivers_are_all_resolvable`` instead of being silently
    counted or silently skipped.

    ``receivers=None`` declares the name unambiguous — pinned, not assumed, by
    ``test_unambiguous_seam_names_have_exactly_one_definition``, which REDs the
    moment a second ``def`` of that name appears anywhere in ``src/``.
    """

    #: The attribute / function name as it appears at a call site.
    name: str
    #: The real function object — imported, not named by string, so a rename
    #: raises at collection time and ``inspect.signature`` reads the live
    #: parameter list (``test_no_exemption_claims_a_channel_that_exists``).
    func: object
    #: Module (relative to ``src/``) the seam is defined in.
    module: str
    #: ``(calling module, receiver expr) -> is-this-seam``. ``None`` ⇒ the name
    #: has exactly one definition in ``src/`` and every call of it resolves here.
    receivers: "dict[tuple[str, str], bool] | None" = None


#: The seams at which a child session's permission envelope is decided: the
#: recorded spawn primitive, the one wrapper that forwards a caller's value into
#: it, and (#3561) the SYNC primitive under both. The wrapper is enumerated
#: because a site that only calls it (``run_agent_step``) still decides the value,
#: and counting the primitive alone would leave that decision outside the gate —
#: which is how #3553 stayed invisible to the #3546 version of this gate for as
#: long as it did.
#:
#: ``AgentRegistry.spawn_session`` joined the list in #3561 on a REACHABILITY
#: criterion, not a shape one. It took no ``narrowing`` argument then, and "it cannot
#: take one, so it is out of scope" is the same shape argument that let #3556
#: through this gate inverted: #3556 passed BECAUSE it spelled ``narrowing=``,
#: while passing a value that was not a function of its parent. A seam with no
#: inheritance channel is an UNMET REQUIREMENT, not an exemption — so it was listed,
#: each of its sites stating what actually decides its child's envelope, and #3562
#: then MET the requirement by adding the channel to the primitive.
_SPAWN_SEAMS: "tuple[_Seam, ...]" = (
    _Seam(
        name="spawn_session_recorded",
        func=AgentRegistry.spawn_session_recorded,
        module="reyn/runtime/registry.py",
    ),
    _Seam(
        name="spawn_ephemeral_session",
        func=spawn_ephemeral_session,
        module="reyn/runtime/session_api.py",
    ),
    _Seam(
        name="spawn_session",
        func=AgentRegistry.spawn_session,
        module="reyn/runtime/registry.py",
        receivers={
            # ``self`` inside registry.py IS the AgentRegistry.
            ("reyn/runtime/registry.py", "self"): True,
            # ``reg = session._registry`` — the AgentRegistry the REPL session holds.
            ("reyn/interfaces/slash/session.py", "reg"): True,
            # ``self.host`` is a RouterLoopHost (RouterHostAdapter in production):
            # a DIFFERENT ``spawn_session``, taking chain_id/mode/request. Its own
            # enclosing function ``router_host_adapter.spawn_session`` is already an
            # enumerated site of the ``spawn_session_recorded`` seam, one hop down.
            ("reyn/runtime/router_loop.py", "self.host"): False,
        },
    ),
)


@dataclass(frozen=True)
class _SiteDeclaration:
    """What a spawn site claims about the value it passes as ``narrowing=``.

    ⚠️ **This is an INTENT record, not a behaviour record.** Nothing here reads the
    site's implementation; a site whose ``parent_layers`` prose and whose code
    disagree stays green in this file forever. That is the exact limit the #3554
    version of this gate had in a different spelling — it pinned that a site passes
    SOME value, which #3553 satisfied while passing a value that was not a function
    of the parent at all. Widening the pin from "passes a value" to "declares which
    parent layers the value composes" moves the failure from silent to *stated*; it
    does not make it detected.

    ``measured_by`` is what closes that: the behavioural half. It names the tests
    that drive the site for real and observe a denied capability's side effect NOT
    happening, so the registry doubles as an index of those tests and a site with an
    empty index is visibly unmeasured rather than invisibly so.

    #3561 split the "no measurement" case in two, because the two were not the same
    thing wearing one field. A FORWARDER has no value of its own to measure and its
    callers do — that is a claim about the call graph, so it is now
    ``measured_via_callers``, a typed list the AST checks against the call graph it
    actually finds (``test_forwarder_exemptions_name_their_real_callers``). The old
    spelling was the prose "there is no value of its own to measure; a thin
    forwarder…" — a JUDGEMENT, which stays green whether or not it is still true, and
    in particular stays green when a second caller appears that nobody measured.
    ``unmeasured_reason`` keeps only the genuinely-unmeasured case.
    """

    #: Which layers of the SPAWNER's envelope the value passed here composes.
    parent_layers: str
    #: ``"tests/<file>.py::<test function>"`` for each behavioural measurement of
    #: this site. Empty only when ``measured_via_callers`` or ``unmeasured_reason``
    #: says why.
    measured_by: "tuple[str, ...]" = ()
    #: (#3561) For a FORWARDER — a site that decides no value of its own — every
    #: site that calls it, each of which must itself be enumerated and must resolve
    #: (transitively, through its own forwarders) to a non-empty ``measured_by``.
    #: Checked against the call graph the AST walk finds, so the exemption cannot
    #: outlive its justification: a new, unmeasured caller REDs the gate.
    measured_via_callers: "tuple[tuple[str, str], ...]" = ()
    #: Why this site has no behavioural measurement, when it has none and is not a
    #: forwarder either.
    unmeasured_reason: str = ""


# Derived from this file's OWN location (never a hardcoded literal): all 5
# narrowing-cluster siblings always move together into the same destination
# bucket, so their repo-relative directory is identical to this file's own
# — computing it here means a future M4 bucket move needs zero string
# updates for these 5 constants, only `git mv` (see #4069 -- this sidesteps
# the class entirely rather than relying on the rename-substitution rule).
_OWN_DIR = Path(__file__).resolve().parent.relative_to(REPO_ROOT).as_posix()
_S3546 = f"{_OWN_DIR}/test_3546_pipeline_driver_narrowing_inheritance.py"
_S3553 = f"{_OWN_DIR}/test_3553_agent_step_worker_narrowing_inheritance.py"
_S3556 = f"{_OWN_DIR}/test_3556_session_spawn_narrowing_inheritance.py"
_S3561 = f"{_OWN_DIR}/test_3561_spawn_session_seam_reachability.py"
_S3562 = f"{_OWN_DIR}/test_3562_slash_session_new_narrowing_inheritance.py"

#: Every spawn site in ``src/``, with the parent layers its ``narrowing=`` value
#: composes and the behavioural test that measures that claim. A site missing from
#: this table fails ``test_every_spawn_site_declares_its_parent_layers`` — the
#: completeness half. See ``_SiteDeclaration`` for why that half is not sufficient.
_SITE_PARENT_LAYERS: "dict[tuple[str, str], _SiteDeclaration]" = {
    ("reyn/runtime/session_api.py", "_spawn_pipeline_driver_session"): _SiteDeclaration(
        parent_layers=(
            "the invoker's #2103-S1a sid-keyed narrowing, VERBATIM "
            "(registry.per_session_narrowing) — this site imposes nothing of its own, "
            "so there is nothing to compose it with. The name-keyed layers (the "
            "agent's permissions declaration, topology capability_profile bindings, "
            "the #2081 _delegate floor) ride along for free because the driver shares "
            "the invoker's identity; the #2285 /visibility toggle and the #1827-S4b "
            "ephemeral untrusted-context narrowing are deliberately not carried — see "
            "this module's docstring, layers 3 and 4."
        ),
        measured_by=(f"{_S3546}::test_narrowed_invoker_pipeline_tool_step",),
    ),
    ("reyn/runtime/session_api.py", "run_agent_step"): _SiteDeclaration(
        parent_layers=(
            "the invoker's #2103-S1a sid-keyed narrowing COMPOSED with this agent "
            "step's own narrowing (the structural delegation deny + the step's "
            "capabilities allow-list), via capability_profile."
            "compose_narrowing_mappings: deny keys union, allow keys intersect, an "
            "absent allow key is ⊤. Same name-keyed / not-carried layers as the "
            "pipeline driver site above (#3553)."
        ),
        measured_by=(
            f"{_S3553}::test_worker_without_capabilities_inherits_invoker_allow_list",
            f"{_S3553}::test_worker_with_capabilities_inherits_invoker_deny_list",
        ),
    ),
    ("reyn/runtime/session_api.py", "spawn_ephemeral_session"): _SiteDeclaration(
        parent_layers=(
            "NONE of its own — a thin forwarder that hands its caller's ``narrowing`` "
            "straight to the primitive. The deciding site is whoever calls IT, which "
            "is why this gate enumerates that seam too."
        ),
        # #3561: was the prose "there is no value of its own to measure; a thin
        # forwarder…" — a judgement no test could check, and one that would have
        # stayed green if a SECOND, unmeasured caller had appeared. The claim is now
        # the call graph itself.
        measured_via_callers=(("reyn/runtime/session_api.py", "run_agent_step"),),
    ),
    ("reyn/runtime/services/router_host_adapter.py", "spawn_session"): _SiteDeclaration(
        parent_layers=(
            "the spawner's #2103-S1a sid-keyed narrowing COMPOSED with the "
            "``session_spawn`` tool argument (i.e. whatever the spawning session's LLM "
            "asked for), via capability_profile.compose_narrowing_mappings: deny keys "
            "union, allow keys intersect, an absent allow key is ⊤ — the same rule as "
            "the agent-step site above, because ``narrowing`` sits where that site's "
            "``capabilities`` sits (an argument the spawner imposes on the child). Same "
            "name-keyed / not-carried layers as the two sites above (#3556). Until "
            "#3556 the LLM's argument was the WHOLE value, which made the tool's own "
            "parameter description — 'restrict-only, cannot widen your envelope' — false."
        ),
        measured_by=(
            f"{_S3556}::test_spawner_deny_survives_an_llm_requested_allow_list",
            f"{_S3556}::test_spawner_allow_list_survives_an_llm_requested_narrowing",
        ),
    ),
    # ── #3561: the sites of the SYNC primitive ``AgentRegistry.spawn_session`` ──
    # #3561 listed these while the primitive had no ``narrowing`` parameter at all,
    # recording that in ``_NARROWING_EXEMPT_SITES`` as an unmet requirement rather than
    # an exemption on the merits — because "it cannot inherit" is not the same claim as
    # "it has nothing to inherit". #3562 met the requirement: the primitive takes the
    # argument, this seam and ``/session new`` pass one, and the three that still do not
    # are exempt on the second claim, stated per site below.
    ("reyn/runtime/registry.py", "spawn_session_recorded"): _SiteDeclaration(
        parent_layers=(
            "NONE of its own — this is the recorded seam itself, calling the sync "
            "primitive and then writing its OWN ``narrowing`` argument to the child's "
            "sid-keyed ``config.yaml`` (the #2103-S1a layer) a few statements later, "
            "through the primitive's ``_persist_session_narrowing`` (#3562 made that "
            "the single writer) and re-injecting it into the live session. It does NOT "
            "hand the value down the primitive's own ``narrowing`` channel, and that is "
            "measured rather than stylistic — see its _NARROWING_EXEMPT_SITES reason. "
            "The value is therefore decided by whoever calls IT, which is why all "
            "three of those callers are enumerated sites in their own right."
        ),
        measured_via_callers=(
            ("reyn/runtime/session_api.py", "spawn_ephemeral_session"),
            ("reyn/runtime/session_api.py", "_spawn_pipeline_driver_session"),
            ("reyn/runtime/services/router_host_adapter.py", "spawn_session"),
        ),
    ),
    ("reyn/runtime/registry.py", "resolve_session"): _SiteDeclaration(
        parent_layers=(
            "NONE, and there is no spawner SESSION here to take them from: this is the "
            "inbound-transport get-or-spawn for a ``<transport>:<native_id>`` "
            "conversation key (a2a / mcp / cron / webhook / web), entered from a "
            "transport frame. The child gets the agent's NAME-keyed layers only (the "
            "agent's permissions declaration, topology capability_profile bindings, the "
            "#2081 _delegate floor), identical to every other session of that agent. "
            "⚠ Not a closed question: ``Session._cross_session_hook_put`` reaches this "
            "with a hook-config ``target_session_id`` of the form ``transport:native``, "
            "so a SESSION can be upstream of it after all — but the session it reaches "
            "through is one of its OWN agent's, whose name-keyed envelope it already "
            "shares, so there is no per-session layer to carry across. A cross-AGENT "
            "variant would change that answer; there is none today. #3562: the "
            "primitive now HAS a ``narrowing`` channel, and this site still passes "
            "nothing — not for want of a channel but for want of a spawner."
        ),
        unmeasured_reason=(
            "the claim is about the ABSENCE of a per-session layer to inherit, and the "
            "behavioural shape this file measures elsewhere (a denied capability's side "
            "effect not happening) has no subject here: with no spawner session there "
            "is no narrowing whose loss could be observed. #3561 recorded this rather "
            "than inventing a measurement whose green would mean nothing. What this "
            "site DOES now get from the primitive — the sid's own persisted narrowing, "
            "applied at construction — is measured at the recovery sites below, which "
            "exercise the same injection in the primitive."
        ),
    ),
    ("reyn/runtime/registry.py", "restore_all"): _SiteDeclaration(
        parent_layers=(
            "NOT an inheritance from a spawner — a RE-ATTACHMENT to the child's own "
            "durable layer. Crash recovery re-creates a session that already existed, "
            "under the SAME sid, and the #2103-S1a narrowing lives in "
            "``<state>/sessions/<enc(sid)>/config.yaml``, keyed by that sid. The "
            "name-keyed layers ride along as everywhere else. "
            "★ This site is why the gate had to widen: enumerating it is what got the "
            "claim MEASURED, and it was false. The layer was resolvable but not "
            "ENFORCED — the factory resolves an envelope with ``sid=None``, so the live "
            "``_contextual_permission`` the RouterLoop reads never saw the file, and a "
            "re-woken narrowed session executed a denied tool for real. #3561 moved the "
            "``#2126`` re-resolve-and-inject into ``spawn_session`` itself, where the "
            "sid becomes known, closing it for every direct caller of the primitive at "
            "once. What the site inherits is now a property of the primitive, which is "
            "the reason the primitive belongs on this list at all. "
            "#3562: the primitive now also has a ``narrowing`` channel, and this site "
            "deliberately passes nothing through it — the primitive PERSISTS what it is "
            "given, so a value here would overwrite the recovering session's own "
            "durable layer with a spawner's, and there is no spawner in a re-wake."
        ),
        measured_by=(
            f"{_S3561}::test_recovery_recreated_session_is_still_inside_its_persisted_narrowing",
            f"{_S3561}::test_the_witness_tool_runs_when_nothing_narrows_it",
        ),
    ),
    ("reyn/runtime/registry.py", "_rewake_pipeline_runs"): _SiteDeclaration(
        parent_layers=(
            "Same as ``restore_all`` above — the pipeline-driver arm of the same "
            "crash-recovery re-creation, re-entering under the work order's "
            "``driver_sid``, so the driver-session's own persisted narrowing (written "
            "when ``_spawn_pipeline_driver_session`` first spawned it through "
            "``spawn_session_recorded``) is resolved from disk by sid and, since #3561, "
            "injected into the live envelope by the primitive both arms share. Measured "
            "by the same behavioural pair, which drives that shared "
            "``spawn_session``-under-an-existing-sid path."
        ),
        measured_by=(
            f"{_S3561}::test_recovery_recreated_session_is_still_inside_its_persisted_narrowing",
            f"{_S3561}::test_the_witness_tool_runs_when_nothing_narrows_it",
        ),
    ),
    ("reyn/interfaces/slash/session.py", "session_cmd"): _SiteDeclaration(
        parent_layers=(
            "the INVOKING session's #2103-S1a sid-keyed narrowing "
            "(``registry.per_session_narrowing`` for the caller's own (agent, sid)), "
            "composed through ``capability_profile.compose_narrowing_mappings`` with a "
            "``None`` child term — this site imposes nothing of its own, so deny ∪ / "
            "allow ∩ / absent-allow-⊤ leaves the invoker's mapping standing (#3562). "
            "Same name-keyed / not-carried layers as the three sites above. Until #3562 "
            "this site carried NOTHING: the child's #2103-S1a layer was empty however "
            "narrow its invoker was, which is why it was declared here as the arc's "
            "open gap rather than left off the list — a site the gate does not "
            "enumerate is a site the gate does not count. "
            "★ The composition is applied UNIFORMLY, with no branch for the case where "
            "the invoking session's agent differs from the ATTACHED agent the child is "
            "born under. ``name`` is ``reg.attached_name``, so on the operator path — "
            "the only face that reaches this site, see below — the caller IS the attach "
            "target and the identities coincide, so there is no live cross-identity "
            "case to branch on; a branch would be a lenient special case for exactly "
            "the caller a uniform restrict-only rule exists to bound. (The original "
            "#3562 argument for uniformity was sharper — the identities differed ONLY "
            "on the model-output path, making 'the identity differs' the same event as "
            "'the escape is happening'. #3595 step 1 retired that argument by closing "
            "the path; it did not change the decision, and #3562/#3586 stands on an "
            "owner policy decision instead: a session opened from a narrowed one should "
            "stay narrowed.) "
            "#3561 measured that this site was REACHABLE FROM MODEL OUTPUT, which is what "
            "first decided it was in scope: an agent step whose prompt was a previous "
            "agent step's model output, run on a session narrowed to a single "
            "capability, reached this site and spawned — no operator in the path, no "
            "LLM call on the reaching turn. ★ #3595 step 1 made that FALSE and the "
            "measurement below is now its inverse: the agent-step prompt rides its own "
            "inbox kind (``TurnOrigin.AGENT_STEP``) instead of claiming ``kind='user'``, "
            "so it never enters ``Session._handle_user_message``'s "
            "``startswith('/')`` dispatch and no registered slash command is reachable "
            "from model output at all. "
            "★ The site STAYS enumerated, and the reason has changed rather than "
            "expired: this list counts every place a child envelope is BORN, not every "
            "place a model can reach — a site whose only caller is an operator is still "
            "a site whose child inherits nothing. What #3595 retires is the SEVERITY "
            "argument, not the entry. The narrowing gap itself was settled separately "
            "by #3562/#3586 on an owner policy decision (a session opened from a "
            "narrowed one should stay narrowed), which stands on its own and never "
            "stood on this reachability. "
            "★ Two different claims are measured below, and they are not "
            "interchangeable. The ``_S3561`` pair is about WHO can reach the site — now "
            "the ABSENCE of the model-output path, paired with an operator control "
            "proving the command itself still runs — and asserts nothing about what the "
            "child inherits. The ``_S3562`` pair is about what the child inherits when "
            "an operator does reach it, witnessed on the side-effect side (a denied "
            "tool's file does not appear) next to its own un-narrowed control."
        ),
        measured_by=(
            f"{_S3561}::test_model_output_cannot_reach_slash_dispatch_and_spawns_nothing",
            f"{_S3561}::test_an_operator_submitted_slash_command_still_spawns_a_session",
            f"{_S3562}::test_a_session_opened_by_slash_session_new_cannot_run_a_tool_its_invoker_is_denied",
            f"{_S3562}::test_the_witness_tool_runs_when_the_invoker_is_not_narrowed",
        ),
    ),
}


_SRC = REPO_ROOT / "src"


@dataclass(frozen=True)
class _CallSite:
    """One resolved call of one seam."""

    module: str          # relative to ``src/``
    function: str        # enclosing function ("<module>" at module level)
    seam: str            # the resolved seam's ``name``
    has_narrowing: bool  # an explicit ``narrowing=`` kwarg, or ``**kwargs`` forwarding

    @property
    def key(self) -> "tuple[str, str]":
        return (self.module, self.function)


#: Every ``(calling module, receiver expr)`` this run saw for a seam that needs
#: resolution but that ``_Seam.receivers`` does not answer. Populated by
#: :func:`_spawn_call_sites` and asserted empty by
#: ``test_spawn_seam_receivers_are_all_resolvable`` — an unresolvable receiver must
#: not be silently counted (a false site) OR silently skipped (a missed site).
_UNRESOLVED_RECEIVERS: "set[tuple[str, str, str]]" = set()


def _spawn_call_sites() -> "list[_CallSite]":
    """Every CALL of a ``_SPAWN_SEAMS`` seam in ``src/``, RESOLVED to a definition.

    An AST walk, not a regex: the same keyword inside a docstring or a comment is
    prose, and matching it textually has already produced false positives on this
    codebase. ``ast`` sees only real ``Call`` nodes.

    #3561: the walk resolves each call by RECEIVER, not by name. Matching
    ``spawn_session`` by name alone attributes ``router_loop.py``'s
    ``self.host.spawn_session(chain_id=…, mode=…, request=…)`` to
    ``AgentRegistry.spawn_session``, which is a different function taking different
    arguments — a false site that would then need a false declaration. A receiver
    the seam's table does not answer is recorded in ``_UNRESOLVED_RECEIVERS`` and
    fails its own test; the walk never guesses in either direction.
    """
    _UNRESOLVED_RECEIVERS.clear()
    by_name: "dict[str, _Seam]" = {s.name: s for s in _SPAWN_SEAMS}
    sites: "list[_CallSite]" = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = str(py.relative_to(_SRC))
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        # Enclosing-function attribution: walk defs, then their nested calls.
        enclosing: "dict[int, str]" = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    enclosing.setdefault(id(child), node.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name, receiver = func.attr, ast.unparse(func.value)
            elif isinstance(func, ast.Name):
                name, receiver = func.id, ""
            else:
                continue
            seam = by_name.get(name)
            if seam is None:
                continue
            if seam.receivers is not None:
                resolved = seam.receivers.get((rel, receiver))
                if resolved is None:
                    _UNRESOLVED_RECEIVERS.add((rel, receiver, name))
                    continue
                if not resolved:
                    continue  # a different function that shares the name
            has_narrowing = any(kw.arg == "narrowing" for kw in node.keywords) or any(
                kw.arg is None for kw in node.keywords  # **kwargs forwarding
            )
            sites.append(_CallSite(
                module=rel,
                function=enclosing.get(id(node), "<module>"),
                seam=seam.name,
                has_narrowing=has_narrowing,
            ))
    return sites


def _definition_sites(name: str) -> "set[str]":
    """Every module in ``src/`` that defines a function or method called ``name``
    — the ground truth an unambiguous-name claim is checked against."""
    found: "set[str]" = set()
    for py in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                found.add(str(py.relative_to(_SRC)))
    return found


def test_spawn_seam_call_sites_are_findable() -> None:
    """Tier 2: instrument check — the AST walk finds the seams' known call sites.

    A completeness gate that silently found NOTHING would pass for the wrong
    reason, so the gate's own search is witnessed against four sites it must see:
    the pipeline driver spawn, the ``session_spawn`` tool's spawn, (#3553) the
    agent-step spawn, which reaches the recorded primitive only through the wrapper,
    and (#3561) ``/session new``, which reaches the SYNC primitive and no wrapper at
    all. The fourth is the one the pre-#3561 name-matching walk could not see,
    because ``spawn_session`` was not on the list to match.
    """
    sites = _spawn_call_sites()
    functions = {s.key for s in sites}
    assert ("reyn/runtime/session_api.py", "_spawn_pipeline_driver_session") in functions
    assert ("reyn/runtime/session_api.py", "run_agent_step") in functions
    assert any(mod == "reyn/runtime/services/router_host_adapter.py" for mod, _ in functions)
    assert ("reyn/interfaces/slash/session.py", "session_cmd") in functions


def test_spawn_seam_resolution_separates_same_named_functions() -> None:
    """Tier 2: (#3561) the walk resolves ``spawn_session`` by RECEIVER, so it does
    not conflate three unrelated functions that share the name.

    The known-present positive and the known-present negative are asserted together,
    because either alone is satisfiable by a broken instrument: a walk that matched
    nothing would pass a negative-only check, and a walk that matched everything by
    name would pass a positive-only one.

      positive — ``interfaces/slash/session.py``'s ``reg.spawn_session(...)`` IS
                 ``AgentRegistry.spawn_session``;
      negative — ``runtime/router_loop.py``'s ``self.host.spawn_session(...)`` is
                 ``RouterHostAdapter.spawn_session`` (``chain_id`` / ``mode`` /
                 ``request``), and the registry's envelope-birth accounting must not
                 claim it. Its own enclosing function is separately enumerated as a
                 site of ``spawn_session_recorded``, one hop down.
    """
    resolved = {(s.module, s.function, s.seam) for s in _spawn_call_sites()}
    assert (
        "reyn/interfaces/slash/session.py", "session_cmd", "spawn_session",
    ) in resolved
    assert not any(
        mod == "reyn/runtime/router_loop.py" and seam == "spawn_session"
        for mod, _fn, seam in resolved
    ), (
        "the walk attributed router_loop.py's host-protocol spawn_session call to "
        "AgentRegistry.spawn_session — a name match, not a resolution"
    )
    # …and the hop down IS counted, so the negative above is a re-attribution, not
    # a hole.
    assert (
        "reyn/runtime/services/router_host_adapter.py", "spawn_session",
        "spawn_session_recorded",
    ) in resolved


def test_spawn_seam_receivers_are_all_resolvable() -> None:
    """Tier 2: (#3561) every call of an ambiguous seam name has a receiver the seam's
    resolution table answers.

    The walk refuses to guess: an unlisted receiver is neither counted (a phantom
    site with a phantom declaration) nor skipped (a real site the gate stops
    counting, which is the failure mode #3561 exists to close). It lands here
    instead, so a new call site through an unfamiliar receiver forces a human to
    resolve it once and record the answer.
    """
    _spawn_call_sites()
    assert not _UNRESOLVED_RECEIVERS, (
        "call(s) of a spawn-seam name whose receiver the seam's `receivers` table "
        f"does not resolve: {sorted(_UNRESOLVED_RECEIVERS)!r}. Resolve each to its "
        "DEFINITION and add it as True (this seam) or False (a different function "
        "of the same name)."
    )


def test_unambiguous_seam_names_have_exactly_one_definition() -> None:
    """Tier 2: (#3561) a seam declared unambiguous (``receivers=None``) really has
    one definition in ``src/``, and every seam's declared module really defines it.

    Name matching is sound only under that premise, and the premise is exactly the
    kind of thing that silently stops being true — someone adds a second
    ``spawn_ephemeral_session`` on another class and the walk starts attributing its
    calls to this one. Asserting the definition SET (not its size) means the failure
    names the intruder.
    """
    for seam in _SPAWN_SEAMS:
        defs = _definition_sites(seam.name)
        assert seam.module in defs, (
            f"seam {seam.name!r} is declared as defined in {seam.module!r}, but no "
            f"such definition was found there (found: {sorted(defs)!r})"
        )
        if seam.receivers is None:
            assert defs == {seam.module}, (
                f"seam {seam.name!r} is declared unambiguous, but {sorted(defs)!r} "
                "define that name — resolution by name alone now conflates them. "
                "Give the seam a `receivers` table."
            )


def test_every_spawn_site_passes_narrowing() -> None:
    """Tier 2: every place a new permission envelope is BORN declares the narrowing
    the child inherits.

    Counted at the place that should inherit, not at the places that might have
    re-checked — the latter set is unbounded, this one is enumerable. A site that
    does not pass ``narrowing=`` registers its reason in ``_NARROWING_EXEMPT_SITES``
    instead of being silently absent.
    """
    missing = [
        s.key for s in _spawn_call_sites()
        if not s.has_narrowing and s.key not in _NARROWING_EXEMPT_SITES
    ]
    assert not missing, (
        "spawn call site(s) do not pass narrowing=, so the spawned session's "
        "permission envelope is born wider than its spawner's: "
        f"{missing!r}. Pass narrowing=, or register the site in "
        "_NARROWING_EXEMPT_SITES with the reason it must not."
    )


def test_no_exemption_claims_a_channel_that_exists() -> None:
    """Tier 2: (#3561, expired and re-grounded by #3562) no exemption rests on the
    claim that the seam it calls cannot take a ``narrowing`` — checked against the LIVE
    signature, so the claim cannot outlive the fact.

    #3561 exempted five sites on exactly that claim, correctly at the time: the sync
    primitive had no such parameter. #3562 added it, which is what this test was built
    to force — two of the five now USE the channel and the other three are re-argued on
    their own merits. What survives here is the mechanical half of that: if a seam takes
    ``narrowing`` and a site is nonetheless exempt, the exemption must not be justified
    by the seam's shape. It also fails on a stale entry (naming a site the walk no
    longer finds), so the registry cannot accumulate dead permissions.

    ⚠️ This test cannot check that a merit-based reason is TRUE — no test can read
    prose. What makes a remaining exemption weigh-able is its ``_SITE_PARENT_LAYERS``
    entry (required by ``test_every_spawn_site_declares_its_parent_layers``) plus the
    behavioural measurement that entry names.
    """
    seam_by_name = {s.name: s for s in _SPAWN_SEAMS}
    sites = _spawn_call_sites()
    for key in sorted(_NARROWING_EXEMPT_SITES):
        reason = _NARROWING_EXEMPT_SITES[key]
        seams_here = {s.seam for s in sites if s.key == key and not s.has_narrowing}
        assert seams_here, (
            f"_NARROWING_EXEMPT_SITES lists {key!r}, but the walk finds no "
            "narrowing-less spawn call there — a stale exemption"
        )
        assert reason.strip(), f"{key!r} is exempt with no stated reason"
        for seam_name in sorted(seams_here):
            params = inspect.signature(seam_by_name[seam_name].func).parameters
            if "narrowing" not in params:
                continue
            assert reason != _UNMET_NO_NARROWING_CHANNEL, (
                f"{key!r} is exempted from passing narrowing= on the grounds that "
                f"{seam_name} has no such parameter, but it DOES — the exemption's "
                "stated reason is false. Pass it, or re-argue the exemption on its "
                "merits."
            )


def test_every_spawn_site_declares_its_parent_layers() -> None:
    """Tier 2: (#3553) every spawn site declares WHICH of its spawner's envelope
    layers the value it passes composes.

    The #3546 gate above pins that a site passes SOME ``narrowing=`` value, which is
    satisfiable by a value that is not a function of the parent at all — exactly what
    ``run_agent_step`` did. This half makes the composition a stated property of each
    site. ⚠️ Stated, not verified: see ``_SiteDeclaration``.
    """
    undeclared = [
        s.key for s in _spawn_call_sites() if s.key not in _SITE_PARENT_LAYERS
    ]
    assert not undeclared, (
        "spawn call site(s) do not declare which layers of the spawner's permission "
        f"envelope their narrowing= value composes: {undeclared!r}. Add an entry to "
        "_SITE_PARENT_LAYERS naming the layers AND the behavioural test that measures "
        "them (or the reason there is none)."
    )


def test_every_declared_site_names_a_behavioural_test_or_a_reason() -> None:
    """Tier 2: (#3553) the declaration registry doubles as an INDEX OF TESTS — each
    site names either the behavioural test(s) that measure its composition or the
    reason it has none, and each named test exists.

    This is the join between the gate's two halves. The completeness half records
    intent and cannot fail on a wrong implementation; the semantic half is per-site
    behavioural and cannot enumerate. Requiring every declared site to point at a
    real test is what stops the intent record from being the only thing a new site
    ever gets — a stale name (a renamed or deleted test) fails here rather than
    quietly leaving the site unmeasured.
    """
    for (mod, fn), decl in sorted(_SITE_PARENT_LAYERS.items()):
        assert decl.parent_layers.strip(), f"{mod}::{fn} declares no parent layers"
        assert (
            decl.measured_by
            or decl.measured_via_callers
            or decl.unmeasured_reason.strip()
        ), (
            f"{mod}::{fn} names neither a behavioural test, nor the callers that "
            "carry the measurement for it, nor a reason it has none"
        )
        for ref in decl.measured_by:
            path_part, _, test_name = ref.partition("::")
            path = REPO_ROOT / path_part
            assert path.is_file(), f"{mod}::{fn} names a missing test file: {ref}"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            defined = {
                node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert test_name in defined, (
                f"{mod}::{fn} names a behavioural test that does not exist: {ref}. "
                "A renamed or deleted measurement must not leave the site silently "
                "declared-but-unmeasured."
            )


def test_forwarder_exemptions_name_their_real_callers() -> None:
    """Tier 2: (#3561) a forwarder's exemption is a claim about the CALL GRAPH, and
    the call graph is what checks it.

    ``spawn_ephemeral_session``'s exemption used to read "there is no value of its
    own to measure; a thin forwarder…" — a judgement, true or false in the reader's
    head and green either way. In particular it stayed green under the one change
    that would falsify it: a SECOND caller, deciding a second value nobody measured.
    The claim is now "these are its callers", and this test compares that list to the
    callers the AST actually finds, in both directions:

      - a declared caller the walk does not find ⇒ the list is stale;
      - a found caller the list omits ⇒ a new deciding site slipped in behind the
        exemption, which is exactly the case the prose could not catch.

    A declared caller must also be an enumerated site itself, so the exemption cannot
    hand the measurement to somewhere the gate never looks.
    """
    sites = _spawn_call_sites()
    for key, decl in sorted(_SITE_PARENT_LAYERS.items()):
        if not decl.measured_via_callers:
            continue
        seam_name = key[1]  # a forwarder's own def name is the seam its callers call
        assert seam_name in {s.name for s in _SPAWN_SEAMS}, (
            f"{key!r} declares measured_via_callers, but {seam_name!r} is not an "
            "enumerated seam — its callers are not something this walk can find"
        )
        actual = {s.key for s in sites if s.seam == seam_name}
        declared = set(decl.measured_via_callers)
        assert declared == actual, (
            f"{key[0]}::{key[1]} claims its measurement is carried by "
            f"{sorted(declared)!r}, but the call graph says its callers are "
            f"{sorted(actual)!r}. A forwarder exemption must not outlive the caller "
            "set that justified it."
        )
        for caller in sorted(declared):
            assert caller in _SITE_PARENT_LAYERS, (
                f"{key[0]}::{key[1]} defers its measurement to {caller!r}, which is "
                "not itself a declared site — the deferral would leave the value "
                "decided outside the gate"
            )


def test_forwarder_deferral_terminates_in_a_real_measurement() -> None:
    """Tier 2: (#3561) following every ``measured_via_callers`` deferral reaches a
    site with a non-empty ``measured_by`` — the exemption chain has a bottom.

    Two forwarders now defer to each other's callers
    (``spawn_session_recorded`` → ``spawn_ephemeral_session`` → ``run_agent_step``),
    and a deferral graph that closed into a cycle, or that ended on an
    ``unmeasured_reason``, would let a site be "measured" by nothing at all while
    every individual link looked accounted for. Walking the closure is what makes
    "its callers are measured individually" a checkable sentence rather than a
    plausible one.
    """
    for key, decl in sorted(_SITE_PARENT_LAYERS.items()):
        if not decl.measured_via_callers:
            continue
        seen: "set[tuple[str, str]]" = set()
        frontier = list(decl.measured_via_callers)
        leaves: "list[tuple[str, str]]" = []
        while frontier:
            cur = frontier.pop()
            if cur in seen:
                continue
            seen.add(cur)
            sub = _SITE_PARENT_LAYERS[cur]
            if sub.measured_via_callers:
                frontier.extend(sub.measured_via_callers)
            else:
                leaves.append(cur)
        assert leaves, (
            f"{key[0]}::{key[1]}'s deferral closes into a cycle and never reaches a "
            "site that measures anything"
        )
        unmeasured = [leaf for leaf in leaves if not _SITE_PARENT_LAYERS[leaf].measured_by]
        assert not unmeasured, (
            f"{key[0]}::{key[1]} defers its measurement, transitively, to site(s) "
            f"that measure nothing: {sorted(unmeasured)!r}"
        )
