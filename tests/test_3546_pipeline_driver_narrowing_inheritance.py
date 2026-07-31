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
passes its LLM's requested narrowing, never its spawner's), declared as such and
filed as #3556 rather than fixed on a guess.

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
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import PresentationWiring
from reyn.runtime.spawn_routing import AuditOnlyNoSurface
from reyn.tools.pipeline_verbs import _handle_run_pipeline
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session

_DENIED_TOOL = "p3546_denied_step"

_PIPELINE_DSL = f"""
pipeline: main
steps:
  - tool: {{name: {_DENIED_TOOL}, args: {{tag: step-ran}}, output: o0}}
"""


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real ``AgentRegistry`` + real ``Session`` factory (the harness shape
    ``tests/test_3093_pipeline_registry_spawn_propagation.py`` uses)."""
    if not (tmp_path / "reyn.yaml").exists():
        (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
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
        events=session._router_host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=session.pipeline_registry,
            agent_registry=reg,
            host=session._router_host,
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
#: site that legitimately must NOT pass ``narrowing=``, with the reason. Empty
#: today: all production call sites pass it. A new site either passes
#: ``narrowing=`` or is registered here with a reason a reviewer can weigh — the
#: #3484 ``*_UNMEASURED`` idiom.
_NARROWING_EXEMPT_SITES: "dict[tuple[str, str], str]" = {}

#: The seams at which a child session's permission envelope is decided: the
#: recorded spawn primitive, plus the one wrapper that forwards a caller's value
#: into it. Both are enumerated because a site that only calls the WRAPPER
#: (``run_agent_step``) still decides the value, and counting the primitive alone
#: would leave that decision outside the gate — which is how #3553 stayed invisible
#: to the #3546 version of this gate for as long as it did.
_SPAWN_SEAMS: "tuple[str, ...]" = ("spawn_session_recorded", "spawn_ephemeral_session")


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
    """

    #: Which layers of the SPAWNER's envelope the value passed here composes.
    parent_layers: str
    #: ``"tests/<file>.py::<test function>"`` for each behavioural measurement of
    #: this site. Empty only when ``unmeasured_reason`` says why.
    measured_by: "tuple[str, ...]" = ()
    #: Why this site has no behavioural measurement, when it has none.
    unmeasured_reason: str = ""


_S3546 = "tests/test_3546_pipeline_driver_narrowing_inheritance.py"
_S3553 = "tests/test_3553_agent_step_worker_narrowing_inheritance.py"

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
        unmeasured_reason=(
            "there is no value of its own to measure; its callers are declared and "
            "measured individually."
        ),
    ),
    ("reyn/runtime/services/router_host_adapter.py", "spawn_session"): _SiteDeclaration(
        parent_layers=(
            "🔴 NONE — the value is the ``session_spawn`` tool argument, i.e. whatever "
            "the spawning session's LLM asked for. The spawner's OWN #2103-S1a "
            "sid-keyed narrowing is not a term, so this is the third site of the "
            "#3546 / #3553 fix-class and it is still open: #3556."
        ),
        unmeasured_reason=(
            "#3556 — reachability is unmeasured (code-trace only), and whether this "
            "seam is even supposed to be restrict-only against its spawner has not "
            "been confirmed against the design prose. Filed rather than fixed in "
            "#3553 so the fix is not a guess."
        ),
    ),
}


def _spawn_call_sites() -> "list[tuple[str, str, int]]":
    """Every CALL of a ``_SPAWN_SEAMS`` function in ``src/`` as
    ``(relative module path, enclosing function, keywords-present marker)``.

    An AST walk, not a regex: the same keyword inside a docstring or a comment is
    prose, and matching it textually has already produced false positives on this
    codebase. ``ast`` sees only real ``Call`` nodes.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    sites: "list[tuple[str, str, int]]" = []
    for py in sorted(src.rglob("*.py")):
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
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name not in _SPAWN_SEAMS:
                continue
            has_narrowing = any(kw.arg == "narrowing" for kw in node.keywords) or any(
                kw.arg is None for kw in node.keywords  # **kwargs forwarding
            )
            sites.append((
                str(py.relative_to(src)),
                enclosing.get(id(node), "<module>"),
                int(has_narrowing),
            ))
    return sites


def test_spawn_seam_call_sites_are_findable() -> None:
    """Tier 2: instrument check — the AST walk finds the seams' known call sites.

    A completeness gate that silently found NOTHING would pass for the wrong
    reason, so the gate's own search is witnessed against three sites it must see:
    the pipeline driver spawn, the ``session_spawn`` tool's spawn, and (#3553) the
    agent-step spawn, which reaches the primitive only through the wrapper.
    """
    sites = _spawn_call_sites()
    functions = {(mod, fn) for mod, fn, _ in sites}
    assert ("reyn/runtime/session_api.py", "_spawn_pipeline_driver_session") in functions
    assert ("reyn/runtime/session_api.py", "run_agent_step") in functions
    assert any(mod == "reyn/runtime/services/router_host_adapter.py" for mod, _ in functions)


def test_every_spawn_site_passes_narrowing() -> None:
    """Tier 2: every place a new permission envelope is BORN declares the narrowing
    the child inherits.

    Counted at the place that should inherit, not at the places that might have
    re-checked — the latter set is unbounded, this one is enumerable. A site that
    legitimately must not pass ``narrowing=`` registers its reason in
    ``_NARROWING_EXEMPT_SITES`` instead of being silently absent.
    """
    missing = [
        (mod, fn) for mod, fn, has in _spawn_call_sites()
        if not has and (mod, fn) not in _NARROWING_EXEMPT_SITES
    ]
    assert not missing, (
        "spawn call site(s) do not pass narrowing=, so the spawned session's "
        "permission envelope is born wider than its spawner's: "
        f"{missing!r}. Pass narrowing=, or register the site in "
        "_NARROWING_EXEMPT_SITES with the reason it must not."
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
        (mod, fn) for mod, fn, _ in _spawn_call_sites()
        if (mod, fn) not in _SITE_PARENT_LAYERS
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
    tests_root = Path(__file__).resolve().parent
    for (mod, fn), decl in sorted(_SITE_PARENT_LAYERS.items()):
        assert decl.parent_layers.strip(), f"{mod}::{fn} declares no parent layers"
        assert decl.measured_by or decl.unmeasured_reason.strip(), (
            f"{mod}::{fn} names neither a behavioural test nor a reason it has none"
        )
        for ref in decl.measured_by:
            path_part, _, test_name = ref.partition("::")
            path = tests_root.parent / path_part
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
