"""Tier 2: FP-0016 Component D — secret_store threading contract.

Covers the plumbing layer: Agent → OSRuntime → ControlIRExecutor /
PreprocessorExecutor → OpContext.secret_store.

Contract pinned:
  - Agent(secret_store=None) constructs without error (default = backward compat)
  - Agent(secret_store=<store>) stores the reference (survives construction)
  - OSRuntime receives secret_store and stores it as _secret_store
  - ControlIRExecutor receives secret_store and stores it as _secret_store
  - ControlIRExecutor._build_ctx() propagates it to OpContext.secret_store
  - PreprocessorExecutor receives secret_store and stores it as _secret_store
  - PreprocessorExecutor._build_op_ctx() propagates it to OpContext.secret_store
  - None propagates as None (no spurious construction)

No MagicMock / AsyncMock / patch of real collaborators.
Uses real ScopedSecretStore, real EventLog, real Workspace, real
ControlIRExecutor, real PreprocessorExecutor.
Agent.run() is NOT called — we test the wiring layer without LLM.
"""
from __future__ import annotations

from pathlib import Path

from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.security.permissions.permissions import PermissionDecl
from reyn.security.secrets.store import ScopedSecretStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_skill() -> Skill:
    """Build the smallest valid Skill that satisfies Pydantic validators."""
    return Skill(
        name="test_skill",
        description="test",
        entry_phase="phase_a",
        phases={
            "phase_a": Phase(
                name="phase_a",
                description="phase_a",
                input_schema={"type": "object", "properties": {}},
                instructions="do nothing",
            )
        },
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="test_output",
        graph=SkillGraph(transitions={}, can_finish_phases=["phase_a"]),
    )


def _make_store(allowed_keys: list[str] | None = None) -> ScopedSecretStore:
    """Construct a real ScopedSecretStore with no file backing (path=None)."""
    return ScopedSecretStore(allowed_keys=allowed_keys or ["MY_KEY"])


def _make_executor(
    tmp_path: Path,
    *,
    secret_store: ScopedSecretStore | None = None,
) -> ControlIRExecutor:
    events = EventLog()
    ws = Workspace(events=events)
    return ControlIRExecutor(
        ws,
        events,
        skill_name="test_skill",
        secret_store=secret_store,
    )


def _make_preprocessor_executor(
    *,
    secret_store: ScopedSecretStore | None = None,
) -> PreprocessorExecutor:
    from reyn.llm.model_resolver import ModelResolver

    events = EventLog()
    ws = Workspace(events=events)
    skill = _make_minimal_skill()
    resolver = ModelResolver({})
    return PreprocessorExecutor(
        skill=skill,
        workspace=ws,
        model="standard",
        events=events,
        subscribers=[],
        resolver=resolver,
        secret_store=secret_store,
    )


# ---------------------------------------------------------------------------
# 1. Agent constructor — secret_store default and explicit
# ---------------------------------------------------------------------------


def test_agent_default_secret_store_is_none() -> None:
    """Tier 2: Agent(secret_store=None) is the default; _secret_store is None."""
    from reyn.agent import Agent

    agent = Agent(model="standard")
    assert agent.secret_store is None


def test_agent_explicit_secret_store_is_stored() -> None:
    """Tier 2: Agent(secret_store=<store>) stores the reference on _secret_store."""
    from reyn.agent import Agent

    store = _make_store(["API_KEY"])
    agent = Agent(model="standard", secret_store=store)
    assert agent.secret_store is store


# ---------------------------------------------------------------------------
# 2. OSRuntime — secret_store stored and propagated to executors
# ---------------------------------------------------------------------------


def test_osruntime_stores_secret_store(tmp_path: Path) -> None:
    """Tier 2: OSRuntime(secret_store=<store>) stores it as _secret_store."""
    from reyn.kernel.runtime import OSRuntime

    skill = _make_minimal_skill()
    store = _make_store(["DB_PASSWORD"])
    runtime = OSRuntime(skill, "standard", secret_store=store)
    assert runtime.secret_store is store


def test_osruntime_propagates_store_to_control_ir_executor(tmp_path: Path) -> None:
    """Tier 2: OSRuntime threads secret_store into ControlIRExecutor.secret_store."""
    from reyn.kernel.runtime import OSRuntime

    skill = _make_minimal_skill()
    store = _make_store(["TOKEN"])
    runtime = OSRuntime(skill, "standard", secret_store=store)
    assert runtime.control_ir_executor.secret_store is store


def test_osruntime_propagates_store_to_preprocessor_executor(tmp_path: Path) -> None:
    """Tier 2: OSRuntime threads secret_store into PreprocessorExecutor.secret_store."""
    from reyn.kernel.runtime import OSRuntime

    skill = _make_minimal_skill()
    store = _make_store(["SECRET_TOKEN"])
    runtime = OSRuntime(skill, "standard", secret_store=store)
    assert runtime.preprocessor.secret_store is store


def test_osruntime_none_store_propagates_as_none(tmp_path: Path) -> None:
    """Tier 2: OSRuntime(secret_store=None) propagates None into all sub-executors."""
    from reyn.kernel.runtime import OSRuntime

    skill = _make_minimal_skill()
    runtime = OSRuntime(skill, "standard")
    assert runtime.secret_store is None
    assert runtime.control_ir_executor.secret_store is None
    assert runtime.preprocessor.secret_store is None


# ---------------------------------------------------------------------------
# 3. ControlIRExecutor — constructor + _build_ctx propagation
# ---------------------------------------------------------------------------


def test_control_ir_executor_default_secret_store_is_none(tmp_path: Path) -> None:
    """Tier 2: ControlIRExecutor() without secret_store defaults _secret_store to None."""
    executor = _make_executor(tmp_path)
    assert executor.secret_store is None


def test_control_ir_executor_stores_secret_store(tmp_path: Path) -> None:
    """Tier 2: ControlIRExecutor(secret_store=<store>) stores on _secret_store."""
    store = _make_store(["API_TOKEN"])
    executor = _make_executor(tmp_path, secret_store=store)
    assert executor.secret_store is store


def test_control_ir_executor_build_ctx_propagates_store(tmp_path: Path) -> None:
    """Tier 2: ControlIRExecutor._build_ctx() sets OpContext.secret_store to the same store."""
    store = _make_store(["MY_KEY"])
    executor = _make_executor(tmp_path, secret_store=store)
    decl = PermissionDecl()
    ctx = executor._build_ctx(decl, current_phase="phase_a")
    assert ctx.secret_store is store


def test_control_ir_executor_build_ctx_none_store_propagates_none(tmp_path: Path) -> None:
    """Tier 2: _build_ctx() with secret_store=None sets OpContext.secret_store to None."""
    executor = _make_executor(tmp_path, secret_store=None)
    decl = PermissionDecl()
    ctx = executor._build_ctx(decl, current_phase="phase_a")
    assert ctx.secret_store is None


# ---------------------------------------------------------------------------
# 4. PreprocessorExecutor — constructor + _build_op_ctx propagation
# ---------------------------------------------------------------------------


def test_preprocessor_executor_default_secret_store_is_none() -> None:
    """Tier 2: PreprocessorExecutor() without secret_store defaults _secret_store to None."""
    executor = _make_preprocessor_executor()
    assert executor.secret_store is None


def test_preprocessor_executor_stores_secret_store() -> None:
    """Tier 2: PreprocessorExecutor(secret_store=<store>) stores on _secret_store."""
    store = _make_store(["WEBHOOK_SECRET"])
    executor = _make_preprocessor_executor(secret_store=store)
    assert executor.secret_store is store


def test_preprocessor_executor_build_op_ctx_propagates_store() -> None:
    """Tier 2: PreprocessorExecutor._build_op_ctx() sets OpContext.secret_store."""
    store = _make_store(["HOOK_KEY"])
    executor = _make_preprocessor_executor(secret_store=store)
    phase = executor._skill.phases["phase_a"]
    ctx = executor._build_op_ctx(phase, step_index=0)
    assert ctx.secret_store is store


def test_preprocessor_executor_build_op_ctx_none_propagates_none() -> None:
    """Tier 2: _build_op_ctx() with secret_store=None → OpContext.secret_store is None."""
    executor = _make_preprocessor_executor(secret_store=None)
    phase = executor._skill.phases["phase_a"]
    ctx = executor._build_op_ctx(phase, step_index=0)
    assert ctx.secret_store is None


# ---------------------------------------------------------------------------
# 5. ScopedSecretStore identity — same object, not a copy
# ---------------------------------------------------------------------------


def test_threading_preserves_identity_not_copy(tmp_path: Path) -> None:
    """Tier 2: secret_store object identity is preserved end-to-end through the wiring.

    The wired object must be the exact same instance (is, not ==) at every
    layer — Agent._secret_store, OSRuntime._secret_store,
    ControlIRExecutor._secret_store, PreprocessorExecutor._secret_store,
    and the OpContext built by _build_ctx / _build_op_ctx.
    No copies or re-construction should occur.
    """
    from reyn.kernel.runtime import OSRuntime

    skill = _make_minimal_skill()
    store = _make_store(["IDENTITY_KEY"])
    runtime = OSRuntime(skill, "standard", secret_store=store)

    # All three layers must hold the exact same object
    assert runtime.secret_store is store
    assert runtime.control_ir_executor.secret_store is store
    assert runtime.preprocessor.secret_store is store

    # OpContext built by ControlIRExecutor must also carry same object
    decl = PermissionDecl()
    ctx = runtime.control_ir_executor._build_ctx(decl, current_phase="phase_a")
    assert ctx.secret_store is store

    # OpContext built by PreprocessorExecutor must also carry same object
    phase = skill.phases["phase_a"]
    pre_ctx = runtime.preprocessor._build_op_ctx(phase, step_index=0)
    assert pre_ctx.secret_store is store
