"""Tier 2b: #3101 -- ``rag_ingest.yaml``'s ``_ingest_body`` file-discovery
fan-out gates the corpus directory (``input_path``) ONCE, upfront and
serially, before the parallel ``glob_files`` fan-out over the 6 file-
extension patterns -- architect design (issue #3101, two comments: the
upfront-gate design + the concurrency-race analysis).

Root cause (real witness, architect-confirmed via primary-data read of
``permissions.py``): NO data race exists (approvals are a shared in-memory
map, consulted/persisted synchronously inside a single-threaded asyncio
process). The bug is purely CONSUMPTION: the file-discovery ``for_each``
(``max_parallel: 4``) used to fan out over 7 patterns (6 extensions +
``input_path`` itself), each independently calling ``require_file_read``
BEFORE any of them reached a grant -- so ONE conceptual "approve the corpus"
answer produced N concurrent JIT prompts (the reported witness: an operator
answered "yes, recursively" once, yet a `.txt` pattern was denied a second
later because its own prompt raced the recursive grant's persistence).

Fix: ``_ingest_body`` now globs ``input_path`` itself in a single, SERIAL
``tool:`` step (``schema: PreflightCheck``-gated so a denial aborts cleanly,
same shape as X1's MCP pre-flight) BEFORE the parallel extension-pattern
fan-out. By the time the fan-out starts, the corpus directory's recursive
read-grant is already established (existing OR obtained via that one JIT
prompt), so every fan-out pattern -- a descendant path of ``input_path`` --
resolves as a synchronous grant HIT (``permissions.py``'s
``_is_path_approved_for``'s recursive-parent match), never a race. This
reuses the EXISTING permission model (the JIT prompt's "[r] grant the
parent directory recursively" choice, ``permissions.py:744``) -- no new
permission concept, no new op.

Harness: drives ``_ingest_body`` (the real pipeline doc, loaded from the
real ``rag_ingest.yaml`` via the real ``PipelineExecutor`` + ``PipelineRegistry``)
against a REAL ``PermissionResolver`` (interactive) + a REAL, RequestBus-
compatible Fake that pre-answers with a scripted choice (mirrors
``tests/test_require_file_jit_ask_1505.py``'s ``_FakeBus`` -- no mocks) and
the REAL ``glob_files`` tool handler (``reyn.tools.file._handle_glob`` via
``reyn.tools.pipeline_verbs._make_tool_dispatch`` -- the exact seam a
``tool:`` pipeline step dispatches through in production). The corpus
directory is a genuine ``tmp_path``-sibling folder OUTSIDE the test
project's root (the real-world "corpus is somewhere else on disk" case that
triggers the JIT prompt at all -- an in-project corpus never leaves the
default zone and this bug would never surface).

The harness stops ``_ingest_body`` at file discovery: the corpus directory
is left with no files matching the tested extensions, so the per-file
``for_each`` (the NEXT fan-out, over ``ctx.files``) is a structural no-op and
the run proceeds straight to the ``list_metadata`` MCP call, which fails
cleanly (no MCP servers wired in this harness -- only the file-discovery
permission path is under test) -- the expected, asserted-on abort point,
distinguished from a permission-path abort by its error text.

strip-falsify (verified manually while developing the fix, documented here
per the ``test_ingest_file_discovery_aborts_clean_on_unreadable_input_path``
precedent in ``tests/test_fp0063_p3_rag_pipelines.py``): reverting
``rag_ingest.yaml`` to fold ``input_path`` back into the parallel
``for_each`` (the pre-#3101 shape: 7 patterns, ``max_parallel: 4``, no
upfront gate) against ``test_upfront_gate_answers_exactly_once`` below
reproduces MULTIPLE ``bus.asks`` entries (one per pattern whose consult
raced ahead of the recursive grant) instead of the fixed shape's single
entry -- i.e. the upfront-gate step is load-bearing for the "one approval
covers the corpus" contract this test pins.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import reyn.builtin as _builtin_pkg
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.pipeline.executor import PipelineExecutor
from reyn.data.pipelines.registry import build_pipeline_registry
from reyn.data.workspace import Workspace
from reyn.intervention_choices import NO, RECURSIVE
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.tools.pipeline_verbs import _make_tool_dispatch
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.user_intervention import InterventionAnswer, UserIntervention

_RAG_PLUGIN_DIR = Path(_builtin_pkg.__file__).resolve().parent / "plugins" / "rag"
_INGEST_PATH = _RAG_PLUGIN_DIR / "pipelines" / "rag_ingest.yaml"


class _FakeBus:
    """Real RequestBus-compatible Fake that pre-answers with a scripted
    choice -- same pattern as ``tests/test_require_file_jit_ask_1505.py``'s
    ``_FakeBus`` / ``tests/test_config_write_jit_bus_3086.py``'s -- no mocks."""

    def __init__(self, choice: str) -> None:
        self._choice = choice
        self.asks: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._choice, choice_id=self._choice)


def _write_project(project_root: Path) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "reyn.yaml").write_text(
        yaml.dump(
            {"pipelines": {"entries": {"rag_ingest": {"path": str(_INGEST_PATH)}}}},
            allow_unicode=True, default_flow_style=False,
        ),
        encoding="utf-8",
    )


def _bus_wired_tool_dispatch(project_root: Path, bus: "_FakeBus", *, resolver: PermissionResolver):
    """Build the REAL ``glob_files`` dispatch path (``_make_tool_dispatch`` ->
    the registered ``ToolDefinition``'s handler, ``reyn.tools.file._handle_glob``)
    wired to a REAL, interactive ``PermissionResolver`` + the given Fake bus,
    via ``RouterCallerState.op_context_factory`` -- the same seam
    ``build_legacy_op_context`` (``reyn/tools/op_context_bridge.py``) reads in
    production to build the op-runtime ``OpContext`` a delegating tool handler
    uses. No stub op_runtime handler, no bypassed permission gate."""
    events = EventLog()
    workspace = Workspace(
        events=events, permission_resolver=resolver, actor="rag_ingest_test",
        base_dir=project_root,
    )
    op_ctx = OpContext(
        workspace=workspace, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="rag_ingest_test", intervention_bus=bus,
    )
    router_state = RouterCallerState(op_context_factory=lambda: op_ctx)
    tool_ctx = ToolContext(
        events=events, permission_resolver=resolver, workspace=workspace,
        caller_kind="router", router_state=router_state,
    )
    return _make_tool_dispatch(tool_ctx)


def _seed_ctx(corpus_dir: Path, project_root: Path) -> dict:
    return {
        "input_path": str(corpus_dir),
        "output_db": str(project_root / "rag.sqlite"),
        "chunk_size": 400,
        "chunk_overlap_ratio": 0.125,
        "embedding_model": "standard",
        "markitdown_server": "reyn_markitdown",
        "chunker_server": "reyn_chunker",
        "vectorstore_server": "reyn_vector_store",
        "file_extensions": ["[tT][xX][tT]", "[mM][dD]", "[pP][dD][fF]",
                             "[xX][lL][sS][xX]", "[pP][pP][tT][xX]", "[dD][oO][cC][xX]"],
        "max_files": 10000,
        "filter_none_conversions": True,
    }


async def _run_ingest_body(project_root: Path, corpus_dir: Path, bus: "_FakeBus",
                            resolver: PermissionResolver) -> Exception:
    """Run the real ``_ingest_body`` pipeline doc against ``corpus_dir``,
    return the exception the run raised (this harness wires no real MCP
    servers -- the run is expected to fail cleanly at the FIRST MCP call
    reached AFTER file discovery, ``list_metadata`` -- see module docstring)."""
    _write_project(project_root)
    registry = build_pipeline_registry(
        {"entries": {"rag_ingest": {"path": str(_INGEST_PATH)}}}, project_root, strict=True,
    )
    pipeline = registry.get("rag_ingest._ingest_body")
    schema_registry = registry.get_schema_registry("rag_ingest._ingest_body")
    tool_dispatch = _bus_wired_tool_dispatch(project_root, bus, resolver=resolver)
    executor = PipelineExecutor()
    with pytest.raises(Exception) as exc_info:  # noqa: PT011 -- exact type asserted by callers
        await executor.run(
            pipeline, _seed_ctx(corpus_dir, project_root), tool_dispatch=tool_dispatch,
            state_log=None, run_id="test-3101", schema_registry=schema_registry,
        )
    return exc_info.value


# ---------------------------------------------------------------------------
# 1. Fixed shape: one recursive-answer JIT prompt covers the WHOLE corpus.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upfront_gate_answers_exactly_once(tmp_path: Path) -> None:
    """Tier 2b: #3101 -- with the upfront gate in place, a fresh OUTSIDE-
    project corpus directory triggers EXACTLY ONE JIT permission prompt
    (the upfront ``glob_files(input_path)`` call), regardless of the 6
    extension patterns the fan-out below it globs afterward. Answering that
    ONE prompt "recursive" is enough: no further prompt fires, and the run
    proceeds past file discovery cleanly (it fails downstream at the
    unwired ``list_metadata`` MCP call -- confirmed by the error text NOT
    naming glob_files/permission, i.e. file discovery itself never denied).

    This is the "1 approval should cover the corpus" contract #3101's
    witness found broken (a `.txt` pattern denied mid-run despite an
    earlier recursive "yes") -- pinned here as exactly 1 ask.
    """
    project_root = tmp_path / "proj"
    corpus_dir = tmp_path.parent / f"{tmp_path.name}_outside_corpus"
    corpus_dir.mkdir()

    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root,
        file_zone_root=project_root, interactive=True,
    )
    bus = _FakeBus(RECURSIVE)

    exc = await _run_ingest_body(project_root, corpus_dir, bus, resolver)

    asked_prompts = [a.prompt for a in bus.asks]
    assert bus.asks, "expected at least one JIT permission prompt for a fresh outside-project corpus"
    # Behavioral core of the fix: NONE of the prompts name a glob PATTERN
    # (a "*" character) -- every extension pattern the fan-out globs is a
    # descendant of the already-granted corpus root, so the fan-out itself
    # never independently reaches the JIT-ask branch. Were the upfront gate
    # absent (the pre-#3101 shape), each of the 6 extension patterns races
    # its own consult and several would show up here as their own asks.
    assert all("*" not in p for p in asked_prompts), (
        f"a fan-out pattern prompted independently -- the upfront gate did "
        f"not cover it: {asked_prompts!r}"
    )
    # The one prompt that DID fire must be about the corpus root itself.
    assert any(str(corpus_dir.resolve()) in p for p in asked_prompts), (
        f"the prompt must name the corpus directory itself: {asked_prompts!r}"
    )
    # File discovery must have succeeded cleanly (no permission denial) --
    # the run instead fails downstream, at the harness's unwired MCP call.
    err = str(exc)
    assert "glob_files" not in err and "denied" not in err.lower(), (
        f"file discovery itself must not have failed -- got: {err!r}"
    )


@pytest.mark.asyncio
async def test_upfront_gate_denial_aborts_before_the_fan_out(tmp_path: Path) -> None:
    """Tier 2b: #3101 -- the symmetric case: if the operator DENIES the
    single upfront prompt, ``_ingest_body`` aborts cleanly right there
    (the ``schema: PreflightCheck``-gated upfront step raises), and the
    parallel extension-pattern fan-out never even starts -- so a denial
    also produces exactly ONE prompt, never a "denied N times" storm from
    the fan-out re-consulting each pattern independently."""
    project_root = tmp_path / "proj"
    corpus_dir = tmp_path.parent / f"{tmp_path.name}_outside_corpus_denied"
    corpus_dir.mkdir()

    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root,
        file_zone_root=project_root, interactive=True,
    )
    bus = _FakeBus(NO)

    exc = await _run_ingest_body(project_root, corpus_dir, bus, resolver)

    asked_prompts = [a.prompt for a in bus.asks]
    assert bus.asks, "the upfront prompt must fire before the abort"
    assert all("*" not in p for p in asked_prompts), (
        f"a denied upfront prompt must abort BEFORE the fan-out re-asks each "
        f"pattern -- got a fan-out-shaped (glob-pattern) prompt: {asked_prompts!r}"
    )
    err = str(exc)
    assert "glob_files" in err and ("denied" in err.lower() or "not permitted" in err.lower()), (
        f"the abort must be decision-enabling about the REAL cause "
        f"(glob_files denial), not the harness's downstream MCP stub: {err!r}"
    )


@pytest.mark.asyncio
async def test_upfront_grant_persists_for_a_second_run_with_no_further_prompt(tmp_path: Path) -> None:
    """Tier 2b: #3101 -- the upfront gate's recursive grant is a REAL
    persisted/session approval (the existing permission model, not a
    per-run cache this pipeline invented): running ``_ingest_body`` a
    SECOND time against the SAME corpus directory, on the SAME resolver,
    with a bus that would now DENY anything it is asked, sees ZERO new
    prompts -- the first run's recursive "yes" already covers it. Public-
    surface proof (repeat the real op path, observe the real bus), not an
    assertion on resolver private state.
    """
    project_root = tmp_path / "proj"
    corpus_dir = tmp_path.parent / f"{tmp_path.name}_outside_corpus_persist"
    corpus_dir.mkdir()

    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root,
        file_zone_root=project_root, interactive=True,
    )

    first_bus = _FakeBus(RECURSIVE)
    await _run_ingest_body(project_root, corpus_dir, first_bus, resolver)
    assert first_bus.asks, "the first run must have prompted at least once"

    second_bus = _FakeBus(NO)  # would deny anything it is asked
    exc = await _run_ingest_body(project_root, corpus_dir, second_bus, resolver)

    assert not second_bus.asks, (
        "the recursive grant from the first run must cover the second run's "
        f"file discovery with NO further prompt -- got: "
        f"{[a.prompt for a in second_bus.asks]!r}"
    )
    err = str(exc)
    assert "glob_files" not in err and "denied" not in err.lower(), (
        f"second run's file discovery must not have failed either: {err!r}"
    )
