"""Tier 2: control-IR op contextual gate (#1912b).

The default (json-mode) skill phase dispatches ops via
``ControlIRExecutor.execute`` — the common path a narrowed agent's skill takes.
#1912b gates that path through the SAME shared check as the chat / phase
RouterLoop (``tool_contextually_denied``), so contextual narrowing is enforced on
every tool path (bypass-impossible by construction).

Pins: (a) the op-kind↔tool-name mapping is EXHAUSTIVE over ALL_OP_KINDS (a gap =
silent bypass); (b) the dangerous deny-set entries reach their ops; (c) the REAL
``execute`` denies a contextually-denied op and an un-narrowed executor does not
(CLEAN RED). No mocks — real ControlIRExecutor + real execute().
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.kernel.control_ir_executor import ControlIRExecutor
from reyn.core.op_runtime.contextual_gate import (
    _OP_KIND_ALIASES,
    op_contextually_denied,
    op_kind_tool_names,
)
from reyn.core.op_runtime.registry import ALL_OP_KINDS
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import SandboxedExecIROp
from reyn.security.permissions.effective import ContextualPermission
from reyn.security.permissions.permissions import PermissionResolver

# ── steer 1: mapping completeness (no silent bypass) ────────────────────────


def test_mapping_covers_all_op_kinds():
    """Tier 2: every op kind is mapped — a missing entry would silently bypass."""
    assert set(_OP_KIND_ALIASES) == set(ALL_OP_KINDS)


def test_op_kind_always_self_named():
    """Tier 2: an op's candidates always include its own kind (un-aliased kinds
    still gate on their name)."""
    for kind in ALL_OP_KINDS:
        assert kind in op_kind_tool_names(kind)


def test_alias_values_are_valid_qualified_names():
    """Tier 2: every alias VALUE is a real qualified name (a key in the dispatch
    map) — so a future rename of a qualified name breaks THIS test, keeping the
    alias map drift-proof. Without this, a stale alias would silently let a
    qualified-named deny leak past the control-IR gate (the keys-only completeness
    test cannot catch a value drift)."""
    from reyn.tools.universal_dispatch import _OPERATION_RULES, _RESOURCE_RULES
    valid = set(_OPERATION_RULES) | set(_RESOURCE_RULES)
    for kind, aliases in _OP_KIND_ALIASES.items():
        for alias in aliases:
            assert alias in valid, f"{kind}: alias {alias!r} is not a current qualified name"


def test_dangerous_deny_entries_reach_their_ops():
    """Tier 2: the built-in untrusted deny-set entries that map to ops do block
    them — via BOTH the qualified and the unwrapped form."""
    # exec: qualified form blocks the sandboxed_exec op
    assert op_contextually_denied(
        ContextualPermission(tool_deny=frozenset({"exec__sandboxed_exec"})), "sandboxed_exec",
    )
    # exec: unwrapped form also blocks it
    assert op_contextually_denied(
        ContextualPermission(tool_deny=frozenset({"sandboxed_exec"})), "sandboxed_exec",
    )
    # mcp install: qualified form blocks the mcp_install op
    assert op_contextually_denied(
        ContextualPermission(tool_deny=frozenset({"mcp__install_registry"})), "mcp_install",
    )
    # router-only deny targets have NO op kind (chat gate covers them)
    assert "delegate_to_agent" not in ALL_OP_KINDS
    assert "remember_shared" not in ALL_OP_KINDS


def test_op_gate_inert_when_no_narrowing():
    """Tier 2: None contextual → never denied (byte-identical)."""
    assert op_contextually_denied(None, "sandboxed_exec") is False
    assert op_contextually_denied(
        ContextualPermission(tool_deny=frozenset({"recall"})), "sandboxed_exec",
    ) is False


# ── steer 3: the REAL execute() gate (CLEAN RED) ────────────────────────────


def _executor(tmp_path: Path, *, contextual=None) -> ControlIRExecutor:
    events = EventLog()
    return ControlIRExecutor(
        workspace=Workspace(events=events),
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={}, project_root=tmp_path, interactive=False,
        ),
        skill_name="s",
        chain_id="c",
        contextual_permission=contextual,
    )


def test_execute_denies_contextually_denied_op(tmp_path, monkeypatch):
    """Tier 2: a narrowed ControlIRExecutor denies a denied op at execute() —
    the same path the default json-mode phase uses. The denied op never reaches
    its handler (the gate returns status='denied' first)."""
    monkeypatch.chdir(tmp_path)
    ex = _executor(
        tmp_path,
        contextual=ContextualPermission(tool_deny=frozenset({"sandboxed_exec"})),
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["echo", "hi"])
    results = asyncio.run(ex.execute([op], phase="p", allowed_ops={"sandboxed_exec"}))
    assert results[0]["status"] == "denied"
    assert results[0]["error"]["kind"] == "tool_excluded"


def test_execute_un_narrowed_does_not_deny(tmp_path, monkeypatch):
    """Tier 2: with no contextual the same op is NOT denied at the gate (CLEAN RED
    contrast — it proceeds past the gate to dispatch). Falsify: if the gate fired
    unconditionally this would be 'denied'."""
    monkeypatch.chdir(tmp_path)
    ex = _executor(tmp_path)  # no contextual
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["echo", "hi"])
    results = asyncio.run(ex.execute([op], phase="p", allowed_ops={"sandboxed_exec"}))
    # not gated as tool_excluded (it may still error later for other reasons,
    # but NOT the contextual denial).
    assert not (
        results[0].get("status") == "denied"
        and results[0].get("error", {}).get("kind") == "tool_excluded"
    )


# ── steer 3: threading flows (OSRuntime → ControlIRExecutor) ─────────────────


def _one_phase_skill():
    from reyn.schemas.models import Phase, Skill, SkillGraph
    p = Phase(name="draft", instructions="d",
              input_schema={"type": "object", "properties": {}}, allowed_ops=["sandboxed_exec"])
    return Skill(
        name="ctx_test", entry_phase="draft", phases={"draft": p},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}}, final_output_name="result",
    )


def test_osruntime_threads_contextual_to_control_ir(tmp_path, monkeypatch):
    """Tier 3a: a narrowed OSRuntime threads contextual to its ControlIRExecutor
    so a denied op is blocked — proving the per-session contextual FLOWS through
    skill execution (Session→SkillRuntime→OSRuntime→ControlIRExecutor), not just
    that the executor gate works in isolation. Falsify: without the OSRuntime→CIE
    thread the op would not be denied (it would dispatch)."""
    from reyn.core.kernel.runtime import OSRuntime
    monkeypatch.chdir(tmp_path)
    rt = OSRuntime(
        _one_phase_skill(), model="stub/model", run_id="r",
        workspace_base_dir=tmp_path,
        contextual_permission=ContextualPermission(tool_deny=frozenset({"sandboxed_exec"})),
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["echo", "hi"])
    results = asyncio.run(
        rt.control_ir_executor.execute([op], phase="draft", allowed_ops={"sandboxed_exec"})
    )
    assert results[0]["status"] == "denied"
    assert results[0]["error"]["kind"] == "tool_excluded"


# ── completeness: the 4th dispatch site — preprocessor run_op (#1912b) ───────


class _NeverRunBackend:
    """Sandbox backend that fails if reached — a denied op must never get here."""

    async def run(self, *a, **k):
        raise AssertionError("a contextually-denied op must NOT reach the sandbox backend")


def test_preprocessor_run_op_is_gated(tmp_path):
    """Tier 2: a narrowed agent's run_op preprocessor step cannot dispatch a
    denied op — the gate fires before execute_op, so the sandbox backend is never
    reached (built-in CLEAN RED: an un-gated path would raise AssertionError)."""
    from reyn.core.kernel.preprocessor_executor import PreprocessorExecutor
    from reyn.schemas.models import Phase, RunOpStep

    events = EventLog()
    phase = Phase(
        name="pp", instructions="d", input_schema={"type": "object", "properties": {}},
        allowed_ops=["sandboxed_exec"],
        preprocessor=[RunOpStep(
            type="run_op",
            op=SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"]),
            into="data._exec", on_error="skip",
        )],
    )
    pe = PreprocessorExecutor(
        skill=_one_phase_skill(),
        workspace=Workspace(events=events, base_dir=tmp_path),
        model="standard", events=events, subscribers=[], resolver=None,
        permission_resolver=PermissionResolver(
            config_permissions={}, project_root=tmp_path, interactive=False),
        sandbox_backend=_NeverRunBackend(),
        contextual_permission=ContextualPermission(tool_deny=frozenset({"sandboxed_exec"})),
    )
    # No AssertionError = the denied op never reached the backend (gated).
    asyncio.run(pe.run(phase, {"type": "x", "data": {}}, None))
    assert any(e.type == "preprocessor_contextually_denied" for e in events.all())
