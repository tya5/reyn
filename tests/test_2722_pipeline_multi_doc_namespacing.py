"""#2722 — multiple ``pipeline:`` documents per DSL file + uniform namespacing.

Namespacing is ALWAYS ON: every registered pipeline's global name is
``{entry-key}.{local-name}`` — no bare registration, regardless of doc count.
The config entry key is a pure namespace label (the old ``key == declared-name``
coupling is gone). ``call``/``match`` targets resolve by a dot/no-dot rule:
dot-less = a same-file sibling (``{key}.name``), dotted = a global reference.

Coverage:
  1. Parser (``parse_pipeline_docs``) — N>=1 pipeline docs; R1 (a reserved '.'
     in a declared name) and R2 (an intra-file duplicate declared name) are
     parse errors; ``parse_pipeline_dsl`` keeps its single-document contract
     (``run_pipeline_inline``'s surface — unchanged).
  2. Loader (``build_pipeline_registry``) — uniform ``{key}.{name}`` for single-
     AND multi-doc files; dot-less sibling resolution + unresolved-sibling
     load error; dotted global left as-is; a reserved '.' in the entry key is a
     load error.
  3. ``pipeline_install`` op (H6) — a multi-doc file's approval-visible result
     enumerates ALL N registered ``{key}.{name}`` global names.

Real instances only (real parser / loader / registry / op handler) — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.op_runtime.context import OpContext
from reyn.core.pipeline.executor import CallStep, MatchStep, PipelineExecutor
from reyn.core.pipeline.parser import (
    PipelineParseError,
    parse_pipeline_docs,
    parse_pipeline_dsl,
)
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.data.pipelines.registry import PipelineLoadError, build_pipeline_registry
from reyn.schemas.models import PipelineInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

# ── helpers ──────────────────────────────────────────────────────────────────


def _write(dir_: Path, filename: str, text: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / filename
    path.write_text(text, encoding="utf-8")
    return path


def _entries(*names_and_paths: "tuple[str, str]") -> dict:
    return {"entries": {name: {"path": path} for name, path in names_and_paths}}


_MULTI_DOC = """
pipeline: main
description: entry point
steps:
  - call: {pipeline: helper, output: r}
---
pipeline: helper
description: private helper
steps:
  - transform: {value: "'helped'", output: out}
"""


# ── 1. Parser ─────────────────────────────────────────────────────────────────


def test_parse_pipeline_docs_returns_all_documents() -> None:
    """Tier 1: parse_pipeline_docs returns EVERY ``pipeline:`` document under its
    bare declared name (config-agnostic — no prefixing at the parser layer)."""
    pipelines = parse_pipeline_docs(_MULTI_DOC, SchemaRegistry())

    assert [p.name for p in pipelines] == ["main", "helper"]
    # bare target verbatim — the parser does NOT prefix (that is the loader's job).
    assert pipelines[0].steps[0].pipeline == "helper"


def test_parse_pipeline_dsl_single_doc_contract_unchanged() -> None:
    """Tier 1: parse_pipeline_dsl keeps its exactly-one-document contract
    (``run_pipeline_inline``'s surface) — a single-doc text parses to one
    Pipeline, a multi-doc text is rejected."""
    single = "pipeline: solo\nsteps:\n  - transform: {value: \"1\", output: o}\n"
    assert parse_pipeline_dsl(single, SchemaRegistry()).name == "solo"

    with pytest.raises(PipelineParseError, match="exactly one"):
        parse_pipeline_dsl(_MULTI_DOC, SchemaRegistry())


def test_r1_dot_in_declared_name_is_parse_error() -> None:
    """Tier 1: R1 — '.' is reserved as the namespace separator, so a declared
    ``pipeline:`` name containing '.' is a parse error."""
    dsl = "pipeline: a.b\nsteps:\n  - transform: {value: \"1\", output: o}\n"
    with pytest.raises(PipelineParseError, match="reserved '.'|namespace separator"):
        parse_pipeline_docs(dsl, SchemaRegistry())


def test_r2_intra_file_duplicate_declared_name_is_parse_error() -> None:
    """Tier 1: R2 — two ``pipeline:`` documents in one file declaring the same
    name is a parse error (both would claim the same ``{key}.name`` global)."""
    dup = (
        "pipeline: same\nsteps:\n  - transform: {value: \"1\", output: o}\n"
        "---\n"
        "pipeline: same\nsteps:\n  - transform: {value: \"2\", output: p}\n"
    )
    with pytest.raises(PipelineParseError, match="duplicate 'pipeline:' name"):
        parse_pipeline_docs(dup, SchemaRegistry())


# ── 2. Loader: uniform namespacing + dot/no-dot resolution ────────────────────


def test_multi_doc_file_registers_every_pipeline_namespaced(tmp_path: Path) -> None:
    """Tier 2: a 2-``pipeline:``-doc file registers BOTH pipelines under the
    uniform ``{key}.{name}`` namespace, and the dot-less sibling ``call`` target
    is rewritten to the sibling's global name."""
    _write(tmp_path / "p", "flow.yaml", _MULTI_DOC)

    registry = build_pipeline_registry(_entries(("orders", "p/flow.yaml")), tmp_path, strict=True)

    assert set(registry.names()) == {"orders.main", "orders.helper"}
    main = registry.get("orders.main")
    assert isinstance(main.steps[0], CallStep)
    assert main.steps[0].pipeline == "orders.helper"  # sibling resolved + namespaced


def test_single_doc_file_also_namespaced_no_bare_exception(tmp_path: Path) -> None:
    """Tier 2: uniform namespacing — a SINGLE-``pipeline:``-doc file registers as
    ``{key}.{name}`` too (no bare-name special case for the single-doc file)."""
    _write(tmp_path / "p", "solo.yaml", "pipeline: solo\nsteps:\n  - transform: {value: \"1\", output: o}\n")

    registry = build_pipeline_registry(_entries(("ns", "p/solo.yaml")), tmp_path, strict=True)

    assert set(registry.names()) == {"ns.solo"}
    assert "solo" not in registry.names()  # no bare registration


def test_dotted_target_is_a_global_reference_left_unchanged(tmp_path: Path) -> None:
    """Tier 2: a DOTTED ``call`` target is a global reference — the loader leaves
    it verbatim (resolved against the whole registry at run time), never
    prefixing it with the entry key."""
    dsl = (
        "pipeline: caller\nsteps:\n"
        "  - call: {pipeline: other_ns.callee, output: r}\n"
    )
    _write(tmp_path / "p", "caller.yaml", dsl)

    registry = build_pipeline_registry(_entries(("mine", "p/caller.yaml")), tmp_path, strict=True)

    caller = registry.get("mine.caller")
    assert caller.steps[0].pipeline == "other_ns.callee"  # global, unchanged


def test_unresolved_dotless_sibling_is_load_error(tmp_path: Path) -> None:
    """Tier 2: a dot-less ``call`` target with no matching same-file sibling is a
    load-time error (fail-loud; NO silent fallback to an unrelated global)."""
    dsl = (
        "pipeline: caller\nsteps:\n"
        "  - call: {pipeline: nonexistent, output: r}\n"
    )
    _write(tmp_path / "p", "caller.yaml", dsl)

    with pytest.raises(PipelineLoadError, match="dot-less call/match target 'nonexistent'"):
        build_pipeline_registry(_entries(("mine", "p/caller.yaml")), tmp_path, strict=True)


def test_r1_dot_in_entry_key_is_load_error(tmp_path: Path) -> None:
    """Tier 2: R1 — a config entry key containing the reserved '.' is a load
    error (it would make the derived ``{key}.{name}`` global name ambiguous)."""
    _write(tmp_path / "p", "solo.yaml", "pipeline: solo\nsteps:\n  - transform: {value: \"1\", output: o}\n")

    with pytest.raises(PipelineLoadError, match="must not contain"):
        build_pipeline_registry(_entries(("a.b", "p/solo.yaml")), tmp_path, strict=True)


def test_match_targets_are_namespaced_recursively(tmp_path: Path) -> None:
    """Tier 2: a ``match`` step's case + default targets follow the SAME dot/
    no-dot rule (dot-less sibling → ``{key}.name``, dotted → global)."""
    dsl = """
pipeline: router
steps:
  - match:
      on: ctx.kind
      cases:
        a: {pipeline: leg}
        b: {pipeline: far_ns.remote}
      default: {pipeline: leg}
---
pipeline: leg
steps:
  - transform: {value: "'leg'", output: o}
"""
    _write(tmp_path / "p", "router.yaml", dsl)

    registry = build_pipeline_registry(_entries(("route", "p/router.yaml")), tmp_path, strict=True)

    router = registry.get("route.router")
    match = router.steps[0]
    assert isinstance(match, MatchStep)
    assert match.cases["a"].pipeline == "route.leg"       # sibling → namespaced
    assert match.cases["b"].pipeline == "far_ns.remote"   # dotted global → unchanged
    assert match.default.pipeline == "route.leg"


@pytest.mark.asyncio
async def test_multi_doc_sibling_call_runs_end_to_end(tmp_path: Path) -> None:
    """Tier 2: the multi-doc file's ``main`` runs its co-located ``helper`` via
    the namespaced sibling reference, end-to-end through the real executor."""
    _write(tmp_path / "p", "flow.yaml", _MULTI_DOC)
    registry = build_pipeline_registry(_entries(("orders", "p/flow.yaml")), tmp_path, strict=True)

    result = await PipelineExecutor().run(
        registry.get("orders.main"), {},
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-2722", pipeline_registry=registry,
    )

    assert result.named_stores["r"] == "helped"


# ── 3. pipeline_install op — H6 enumerate-all-names ───────────────────────────


class _Events:
    def __init__(self) -> None:
        self.emitted: "list[tuple[str, dict]]" = []

    def emit(self, kind: str, **kwargs) -> None:
        self.emitted.append((kind, kwargs))


class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


def _install_ctx(tmp_path: Path) -> "tuple[OpContext, _Events]":
    config_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)
    resolver.session_approve_path(str(config_path), "test", "file.write")
    decl = PermissionDecl(file_write=[{"path": str(config_path), "scope": "just_path"}])
    events = _Events()
    ctx = OpContext(
        workspace=_StubWorkspace(base_dir=tmp_path),
        events=events,
        permission_decl=decl,
        permission_resolver=resolver,
        actor="test",
        intervention_bus=None,
        subscribers=[],
        state_log=None,
    )
    return ctx, events


@pytest.mark.asyncio
async def test_pipeline_install_multi_doc_enumerates_all_names(tmp_path: Path) -> None:
    """Tier 2: H6 — installing a multi-``pipeline:``-doc file enumerates EVERY
    registered ``{key}.{name}`` global name in the approval-visible result and
    the audit event (no silent scope creep behind one approved op.name)."""
    from reyn.core.op_runtime.pipeline_install import handle

    dsl_path = _write(tmp_path / "src", "flow.yaml", _MULTI_DOC)
    ctx, events = _install_ctx(tmp_path)

    op = PipelineInstallIROp(kind="pipeline_install", path=str(dsl_path), name="orders")
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", result
    assert result["name"] == "orders"  # the namespace key
    assert set(result["registered_names"]) == {"orders.main", "orders.helper"}

    installed = [kw for kind, kw in events.emitted if kind == "pipeline_installed"]
    assert installed and set(installed[0]["registered_names"]) == {"orders.main", "orders.helper"}
