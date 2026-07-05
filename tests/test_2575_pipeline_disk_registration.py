"""#2575 — production pipeline registration: disk dir → session PipelineRegistry.

Before #2575 a ``Session`` owned an EMPTY ``PipelineRegistry`` and nothing in
production ever populated it, so no pipeline was launchable in a real session.
This slice adds the disk loader (``reyn.data.pipelines.registry.
build_pipeline_registry``): an operator drops Appendix-B DSL ``*.yaml`` files
into ``pipelines/`` (the default scan dir) and each is parsed + registered under
its OWN declared ``pipeline:`` name at session-factory time.

Coverage:
  1. Contract — the parser now carries the declared name on ``Pipeline.name``,
     and serde round-trips it (default-tolerant for pre-existing on-disk data).
  2. Loader — disk dir → registry keyed by the declared name (filename is a
     container); malformed / duplicate → FAIL LOUD with the path; empty → empty.
  3. Wiring — ``from_config(config, project_root)`` builds the registry once;
     ``Session(pipeline_registry=)`` adopts it.
  4. Surfacing + invoke — a disk-loaded pipeline surfaces as ``pipeline__<name>``
     (list_actions) and runs end-to-end through the real router loop.
  5. Cross-pipeline ``call`` — a loaded pipeline whose step ``call``s ANOTHER
     loaded pipeline resolves + runs (the foundation's deferred named-callee
     production registration, now closed).
  6. Security invariant — the untrusted + delegate floors STILL deny the
     pipeline launch verbs, independent of what is registered (loading a
     pipeline does not loosen the floor).

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
unittest.mock. The LLM in the full-loop test is a real async callable stub
(Tier 2c), the designed seam, mirroring test_pipeline_is5_surfacing.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, PipelineExecutor, TransformStep
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.core.pipeline.serde import pipeline_from_dict, pipeline_to_dict
from reyn.data.pipelines.registry import PipelineLoadError, build_pipeline_registry
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session

# ── helpers ──────────────────────────────────────────────────────────────────


def _write(dir_: Path, filename: str, text: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / filename).write_text(text, encoding="utf-8")


_HELLO_DSL = """
pipeline: hello
description: greet the seed name
steps:
  - transform: {value: "'Hello, ' + ctx.name + '!'", output: greeting}
"""


# ── 1. Contract: parser carries the declared name; serde round-trips it ───────


def test_parser_populates_declared_name_on_pipeline() -> None:
    """Tier 1: parse_pipeline_dsl records the declared ``pipeline:`` name on
    ``Pipeline.name`` — the authoritative key the disk loader registers under
    and a ``call``/``match`` step resolves against."""
    from reyn.core.pipeline.parser import parse_pipeline_dsl

    pipeline = parse_pipeline_dsl(_HELLO_DSL, SchemaRegistry())

    assert pipeline.name == "hello"


def test_pipeline_name_serde_round_trips_non_default_value() -> None:
    """Tier 1: a non-default ``name`` survives pipeline_to_dict → from_dict
    (the work-order / invocation.json persistence used for recovery)."""
    original = Pipeline(
        steps=[TransformStep(value="1 + 1", output="two")],
        description="d",
        name="my_pipeline",
    )

    restored = pipeline_from_dict(pipeline_to_dict(original))

    assert restored.name == "my_pipeline"
    assert restored == original


def test_pipeline_from_dict_defaults_name_when_absent() -> None:
    """Tier 1: an invocation.json persisted before the ``name`` field existed
    (no ``name`` key) decodes to ``""`` — default-tolerant, never a KeyError."""
    legacy = {"description": "d", "steps": [{"kind": "transform", "value": "1", "output": "o"}]}

    restored = pipeline_from_dict(legacy)

    assert restored.name == ""


# ── 2. Loader: disk → registry, keyed by declared name; fail-loud ─────────────


def test_loader_registers_pipeline_from_disk_under_declared_name(tmp_path: Path) -> None:
    """Tier 2: a DSL file under the scan dir is parsed + registered under its
    declared ``pipeline:`` name, with its name + description surfaced via
    ``entries()`` (what the catalog enumerator reads)."""
    _write(tmp_path / "pipelines", "hello.yaml", _HELLO_DSL)

    registry = build_pipeline_registry({"scan_dirs": ["pipelines"]}, tmp_path)

    assert set(registry.names()) == {"hello"}
    assert registry.entries() == (("hello", "greet the seed name"),)
    assert registry.get("hello").name == "hello"


def test_loader_default_scan_dir_is_pipelines(tmp_path: Path) -> None:
    """Tier 2: an absent ``scan_dirs`` falls back to the default ``pipelines/``
    dir (the documented default), so a bare ``pipelines: {}`` still loads."""
    _write(tmp_path / "pipelines", "hello.yaml", _HELLO_DSL)

    registry = build_pipeline_registry({}, tmp_path)

    assert set(registry.names()) == {"hello"}


def test_loader_keys_on_declared_name_not_filename(tmp_path: Path) -> None:
    """Tier 2: the declared ``pipeline:`` name is authoritative — a file named
    ``greet.yaml`` declaring ``pipeline: hello`` registers as ``hello`` (the
    identity a ``call`` resolves against), NOT under the file stem. Registering
    under the filename while ``call`` resolves the declared name would be a
    call that can never find its target."""
    _write(tmp_path / "pipelines", "greet.yaml", _HELLO_DSL)  # file stem = "greet"

    registry = build_pipeline_registry({"scan_dirs": ["pipelines"]}, tmp_path)

    assert set(registry.names()) == {"hello"}  # declared name, not "greet"


def test_loader_malformed_file_fails_loudly_with_path(tmp_path: Path) -> None:
    """Tier 2: a malformed DSL file raises PipelineLoadError naming the file —
    NOT a silent skip (a typo must not silently drop a pipeline the operator
    meant to ship)."""
    _write(tmp_path / "pipelines", "broken.yaml", "pipeline: broken\nsteps: not-a-list\n")

    with pytest.raises(PipelineLoadError) as exc:
        build_pipeline_registry({"scan_dirs": ["pipelines"]}, tmp_path)

    assert "broken.yaml" in str(exc.value)


def test_loader_duplicate_declared_name_fails_loudly(tmp_path: Path) -> None:
    """Tier 2: two files declaring the SAME ``pipeline:`` name surface the
    registry's re-registration guard as a loud load error (with the offending
    file path), never a silent last-wins overwrite."""
    _write(tmp_path / "pipelines", "a.yaml", _HELLO_DSL)
    _write(tmp_path / "pipelines", "b.yaml", _HELLO_DSL)  # also declares "hello"

    with pytest.raises(PipelineLoadError) as exc:
        build_pipeline_registry({"scan_dirs": ["pipelines"]}, tmp_path)

    assert "hello" in str(exc.value)


def test_loader_empty_config_yields_empty_registry(tmp_path: Path) -> None:
    """Tier 2: ``None`` (util/no-root path) → empty. An empty ``{}`` block scans
    the default ``pipelines/`` dir but yields empty here because that dir is
    absent — byte-identical to the pre-#2575 own-constructed empty registry."""
    assert build_pipeline_registry(None, tmp_path).names() == ()
    assert build_pipeline_registry({}, tmp_path).names() == ()  # dir absent → nothing


def test_loader_missing_scan_dir_is_not_an_error(tmp_path: Path) -> None:
    """Tier 2: a configured scan dir that does not exist yet is not an error
    (the operator may add files later) — it simply contributes nothing."""
    registry = build_pipeline_registry({"scan_dirs": ["does_not_exist"]}, tmp_path)

    assert registry.names() == ()


# ── 3. Wiring: from_config build-once + Session adoption ──────────────────────


def test_from_config_builds_populated_registry_from_project_root(tmp_path: Path) -> None:
    """Tier 2: SessionFactoryConfig.from_config(config, project_root) builds the
    registry ONCE from ``config.pipelines`` + the project root — the build-once
    locus (mirrors the available_skills snapshot)."""
    from reyn.config.loader import load_config
    from reyn.runtime.factory_config import SessionFactoryConfig

    _write(tmp_path / "pipelines", "hello.yaml", _HELLO_DSL)
    config = load_config()  # no reyn.yaml → default pipelines: {} → default scan dir

    fc = SessionFactoryConfig.from_config(config, tmp_path)

    assert set(fc.pipeline_registry.names()) == {"hello"}


def test_from_config_without_project_root_is_empty(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: from_config(config) with no project_root → an EMPTY registry even
    if a pipelines/ dir exists at cwd — the util/test path stays byte-identical
    to pre-#2575 (no accidental disk scan without an explicit root)."""
    from reyn.config.loader import load_config
    from reyn.runtime.factory_config import SessionFactoryConfig

    _write(tmp_path / "pipelines", "hello.yaml", _HELLO_DSL)
    monkeypatch.chdir(tmp_path)

    fc = SessionFactoryConfig.from_config(load_config())

    assert fc.pipeline_registry.names() == ()


def test_session_adopts_passed_pipeline_registry(tmp_path: Path) -> None:
    """Tier 2: Session(pipeline_registry=X) adopts X as its live registry (the
    factory threads the disk-loaded one); None → its own empty registry."""
    from reyn.runtime.session import Session

    loaded = PipelineRegistry()
    loaded.register("hello", Pipeline(steps=[TransformStep(value="1", output="o")], name="hello"))

    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "wal.jsonl"),
        pipeline_registry=loaded,
    )

    assert session.pipeline_registry is loaded
    assert session.router_host.get_pipeline_registry() is loaded


def test_session_without_registry_owns_empty_one(tmp_path: Path) -> None:
    """Tier 2: a direct/test Session with no pipeline_registry falls back to its
    own empty PipelineRegistry — byte-identical to pre-#2575."""
    from reyn.runtime.session import Session

    session = Session(agent_name="a", state_log=StateLog(tmp_path / "wal.jsonl"))

    assert isinstance(session.pipeline_registry, PipelineRegistry)
    assert session.pipeline_registry.names() == ()


# ── 4. Surfacing: a disk-loaded pipeline shows as pipeline__<name> ────────────


class _FakeHost:
    """Minimal RouterLoopHost stub — real shape, no mock framework (mirrors
    test_pipeline_is5_surfacing.py's precedent)."""

    agent_name: str = "test-agent"
    agent_role: str = ""
    output_language: str = "en"

    def __init__(self, *, pipeline_registry: Any) -> None:
        self._pipeline_registry = pipeline_registry

        class _E:
            def emit(self, *a, **kw): pass
            subscribers: list = []
        self._events = _E()
        self.get_pipeline_registry = lambda: self._pipeline_registry  # type: ignore[method-assign]
        self.get_agent_registry = lambda: None  # type: ignore[method-assign]

    @property
    def events(self): return self._events

    def get_universal_wrappers_enabled(self) -> bool: return True
    def get_action_usage_tracker(self): return None
    def get_action_embedding_index(self): return None
    def get_embedding_provider(self): return None
    def get_embedding_model_class(self): return None
    def get_action_retrieval_config(self): return None
    def list_available_skills(self) -> list[dict]: return []
    def list_available_agents(self) -> list[dict]: return []
    def get_memory_index(self) -> dict: return {"status": "not_found", "content": ""}
    def get_file_permissions(self): return None
    def get_mcp_servers(self) -> list[dict]: return []
    def get_web_fetch_allowed(self) -> bool: return False
    def get_project_context(self) -> str: return ""
    def get_sandbox_backend(self): return None
    def resolve_model(self, name: str) -> str: return "fake-model"


@pytest.mark.asyncio
async def test_disk_loaded_pipeline_surfaces_in_list_actions(tmp_path: Path) -> None:
    """Tier 2: a pipeline loaded from disk surfaces as ``pipeline__<name>`` with
    its own description via list_actions(category=["pipeline"]) — the enumerate
    path the default scheme flat-lists. Proves disk-load → catalog end-to-end."""
    from reyn.runtime.router_loop import RouterLoop
    from reyn.tools.types import ToolContext
    from reyn.tools.universal_catalog import LIST_ACTIONS

    _write(tmp_path / "pipelines", "hello.yaml", _HELLO_DSL)
    registry = build_pipeline_registry({"scan_dirs": ["pipelines"]}, tmp_path)

    loop = RouterLoop(host=_FakeHost(pipeline_registry=registry), chain_id="c1", router_model="standard")
    rs = await loop._build_router_caller_state()
    ctx = ToolContext(
        events=loop.host.events, permission_resolver=None, workspace=None,
        caller_kind="router", router_state=rs,
    )

    result = await LIST_ACTIONS.handler({"category": ["pipeline"]}, ctx)

    items = {it["qualified_name"]: it for it in result["items"]}
    assert "pipeline__hello" in items
    assert items["pipeline__hello"]["short_description"] == "greet the seed name"


# ── 5. Cross-pipeline call: a loaded pipeline calls another loaded pipeline ────


@pytest.mark.asyncio
async def test_disk_loaded_pipeline_call_resolves_and_runs_end_to_end(tmp_path: Path) -> None:
    """Tier 2: two pipelines loaded FROM DISK — one whose ``call`` step targets
    the other by its declared name — resolve + run end-to-end. This closes the
    foundation's deferred named-callee production registration: a ``call``
    against a disk-registered pipeline now works in production."""
    callee_dsl = """
pipeline: inner
description: inner callee
steps:
  - transform: {value: "ctx.seed + '-inner'", output: out}
"""
    caller_dsl = """
pipeline: outer
description: calls inner
steps:
  - call: {pipeline: inner, pass: [seed], output: called}
  - transform: {value: "ctx.called + '-outer'", output: final}
"""
    _write(tmp_path / "pipelines", "inner.yaml", callee_dsl)
    _write(tmp_path / "pipelines", "outer.yaml", caller_dsl)

    registry = build_pipeline_registry({"scan_dirs": ["pipelines"]}, tmp_path)
    outer = registry.get("outer")

    result = await PipelineExecutor().run(
        outer, {"seed": "x"},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None,
        run_id="run-2575-call",
        pipeline_registry=registry,  # the SAME disk-loaded registry resolves the callee
    )

    assert result.named_stores["called"] == "x-inner"
    assert result.named_stores["final"] == "x-inner-outer"


# ── 6. Security invariant: the floors STILL deny the launch verbs ─────────────


_PIPELINE_LAUNCH_VERBS = (
    "run_pipeline", "pipeline__run",
    "run_pipeline_async", "pipeline__run_async",
    "run_pipeline_inline", "pipeline__run_inline",
    "run_pipeline_inline_async", "pipeline__run_inline_async",
)


@pytest.mark.parametrize("profile_factory_name", ["builtin_untrusted_profile", "builtin_delegate_profile"])
def test_floor_still_denies_pipeline_launch_verbs(profile_factory_name: str) -> None:
    """Tier 2: BOTH the untrusted-content floor and the unbound-delegate floor
    deny every pipeline launch verb (bare + qualified, sync/async/inline) — the
    #2575 security invariant. This slice adds a POPULATION path; it must not
    loosen the floor. A regression here (floor drops a pipeline verb) means an
    untrusted-content turn / an unbound delegate could launch a pipeline (a
    cost-bound multi-step spawn)."""
    import reyn.security.permissions.capability_profile as cp
    from reyn.security.permissions.effective import CapabilityAxis, ContextualLayer

    profile = getattr(cp, profile_factory_name)()
    contextual, _ = cp.resolve_profile(profile)
    layer = ContextualLayer(contextual)

    for verb in _PIPELINE_LAUNCH_VERBS:
        assert layer.allows(CapabilityAxis.TOOL, verb) is False, verb


def test_loading_a_pipeline_does_not_loosen_the_floor(tmp_path: Path) -> None:
    """Tier 2: the floor is registration-INDEPENDENT — a populated registry
    (a real disk-loaded pipeline) does not create a bypass. The resolved
    untrusted floor still denies the launch verbs the loaded pipeline would be
    reached through (``pipeline__<name>`` curries to the denied run_pipeline
    target)."""
    import reyn.security.permissions.capability_profile as cp
    from reyn.security.permissions.effective import CapabilityAxis, ContextualLayer

    _write(tmp_path / "pipelines", "hello.yaml", _HELLO_DSL)
    registry = build_pipeline_registry({"scan_dirs": ["pipelines"]}, tmp_path)
    assert "hello" in registry.names()  # a pipeline IS registered

    contextual, _ = cp.resolve_profile(cp.builtin_untrusted_profile())
    layer = ContextualLayer(contextual)

    # the loaded pipeline resolves (D19) to the run_pipeline target — still denied
    assert layer.allows(CapabilityAxis.TOOL, "run_pipeline") is False
    assert layer.allows(CapabilityAxis.TOOL, "pipeline__run") is False


# ── 4b. Full live loop: disk-loaded pipeline invoked via pipeline__<name> ─────


_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _make_llm_stub(results: list[LLMToolCallResult]):
    call_count = [0]

    async def _stub(**kwargs) -> LLMToolCallResult:
        idx = call_count[0]
        call_count[0] += 1
        return results[idx] if idx < len(results) else results[-1]

    return _stub


@pytest.mark.asyncio
async def test_disk_loaded_pipeline_invokable_through_full_live_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2c: an agent launches a DISK-LOADED pipeline through the REAL router
    loop via the ``pipeline__<name>`` form the enumerator advertises, and its
    real transform output round-trips into chat history — the disk→surface→
    invoke chain end-to-end (not the handler in isolation)."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "pipelines", "hello.yaml", _HELLO_DSL)
    loaded = build_pipeline_registry({"scan_dirs": ["pipelines"]}, tmp_path)

    state_log = StateLog(tmp_path / ".reyn" / "state.wal")
    holder: dict = {}

    def _factory(profile) -> Session:
        return Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            chat_tool_use_scheme="universal-category",
            pipeline_registry=loaded,  # the disk-loaded registry, threaded as the factory would
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("test_agent")
    session = reg.get_or_load("test_agent")
    session.is_attached = True

    invoke = LLMToolCallResult(
        content=None,
        tool_calls=[{
            "id": "tc_1", "type": "function",
            "function": {
                "name": "invoke_action",
                "arguments": json.dumps({
                    "action_name": "pipeline__hello",
                    "args": {"input": {"name": "Reyn"}},
                }),
            },
        }],
        finish_reason="tool_calls", usage=_EMPTY_USAGE,
    )
    text = LLMToolCallResult(
        content="done", tool_calls=[], finish_reason="stop", usage=_EMPTY_USAGE,
    )
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _make_llm_stub([invoke, text]),
    )

    await session._handle_user_message("run hello", chain_id="chain-2575")

    tool_messages = [m for m in session.history if m.role == "tool"]
    assert tool_messages, "expected a tool-result history entry"
    payload = json.loads(tool_messages[-1].content)
    assert payload["status"] == "ok"
    inner = payload["data"]
    assert inner["status"] == "ok"
    assert inner["data"]["output"] == "Hello, Reyn!"
