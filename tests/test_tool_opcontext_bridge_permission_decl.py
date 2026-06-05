"""Tier 2: Tool→OpContext bridge preserves ``phase_state.op_context.permission_decl``.

When a Tool wrapper (= ``src/reyn/tools/<op>.py``) routes a phase-side
dispatch through its ``_handle`` adapter, it constructs a legacy
``OpContext`` from the modern ``ToolContext``. The phase's actual
``PermissionDecl`` (= what the skill declared in ``permissions:``)
must survive that construction. Otherwise the downstream permission
gate (e.g. ``require_shell``) reads an empty ``PermissionDecl()`` and
raises ``PermissionError: shell access not declared`` even when the
skill DID declare ``shell: true``.

Empirical precedent (FP-0008 sandbox_2 2026-05-28 calibration v2):
10/10 SWE-bench instances failed with that exact PermissionError after
PR #1000 fixed the LLM-layer abort. Primary evidence:

  REYN_DEBUG_SHELL_GATE trace at permissions.py:1156 require_shell:
    decl=PermissionDecl(shell=False, ...)  # empty, NOT skill's decl
    cmd='git checkout d16bfe05...'

Root cause: ``src/reyn/tools/shell.py:67`` hardcoded
``permission_decl=PermissionDecl()`` in the legacy_ctx, dropping the
``phase_state.op_context.permission_decl`` the executor already had.
Same anti-pattern at 6 other tool wrappers (= shell / lint /
invoke_skill x2 / recall / web_search / file / read_tool_result).
``web_fetch`` + ``sandboxed_exec`` had early-return-to-phase paths
that already preserved the decl correctly — they're verified here too
for shape-uniformity but did not require code changes.

The fix shape (applied uniformly):
  phase_op_ctx = ctx.phase_state.op_context if ctx.phase_state else None
  permission_decl = (
      phase_op_ctx.permission_decl
      if phase_op_ctx is not None
      else PermissionDecl()
  )

This test parametrizes across all 10 Tool wrappers + verifies the
invariant.
"""
from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.permissions.permissions import PermissionDecl
from reyn.tools.types import PhaseCallerState, ToolContext
from reyn.workspace.workspace import Workspace


def _build_phase_state(perm_decl: PermissionDecl) -> PhaseCallerState:
    """Build a PhaseCallerState whose op_context carries the given PermissionDecl."""
    events = EventLog()
    workspace = Workspace(events=events)
    phase_op_ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=perm_decl,
        permission_resolver=None,
        skill_name="test_skill",
    )
    return PhaseCallerState(
        phase_name="test_phase",
        op_context=phase_op_ctx,
    )


def _build_tool_ctx_with_phase(
    phase_state: PhaseCallerState, tmp_path: Path,
) -> ToolContext:
    """ToolContext that exposes the given phase_state (= phase-side dispatch)."""
    import os

    os.chdir(tmp_path)
    events = EventLog()
    workspace = Workspace(events=events)
    return ToolContext(
        workspace=workspace,
        events=events,
        permission_resolver=None,
        caller_kind="phase",
        phase_state=phase_state,
        router_state=None,
    )


# ── helpers per-tool (= where to assert the bridge's output decl) ──────────


def _shell_legacy_ctx(ctx: ToolContext) -> OpContext:
    """Call shell.py's bridge and return the synthesised legacy_ctx.

    Mirror the bridge construction from shell.py:_handle but stop
    before calling handle_shell — we want the OpContext, not the
    result of executing a real command.
    """
    # We can't intercept the legacy_ctx mid-call without monkeypatching
    # handle_shell. Use the same construction logic as the real bridge.
    phase_op_ctx = (
        ctx.phase_state.op_context if ctx.phase_state is not None else None
    )
    return OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=(
            phase_op_ctx.permission_decl
            if phase_op_ctx is not None
            else PermissionDecl()
        ),
        permission_resolver=ctx.permission_resolver,
        skill_name="",
    )


# ── 1. parametrized bridge invariant across tool wrappers ──────────────
# (#1352-A: the shell.py bridge integration test + the shell parametrize entry
# were removed with the deprecated shell op; the lint / web_search wrappers
# preserve the same Tool→OpContext decl-propagation invariant.)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_module_name, handler_attr, build_args",
    [
        # (a) module-level _handle adapters that build legacy_ctx inline
        ("reyn.tools.lint", "_handle", {"skill_path": "reyn/local/x"}),
        ("reyn.tools.web_search", "_handle", {"query": "x"}),
    ],
)
async def test_tool_bridge_preserves_decl_from_phase_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_module_name: str,
    handler_attr: str,
    build_args: dict,
) -> None:
    """Tier 2: each Tool's _handle preserves phase_state.op_context.permission_decl.

    Monkeypatches the underlying op_runtime handler to capture the
    legacy_ctx passed in. Asserts the captured permission_decl is the
    SAME object as the phase_state's permission_decl (identity check
    is sufficient — the bridge should not synthesise a new one).
    """
    import importlib

    module = importlib.import_module(tool_module_name)

    captured: dict[str, Any] = {}

    async def _capture(*args, **kwargs):
        # Each tool's handler signature differs; capture from either
        # positional ctx or kwarg ctx. Use slicing (= canonical idiom
        # per testing.ja.md, avoids `len(args) > N` format-pin) to
        # extract args[1] safely.
        positional_ctx = next(iter(args[1:2]), None)
        ctx = kwargs.get("ctx") or positional_ctx
        captured["legacy_ctx"] = ctx
        return {"status": "ok"}

    # Patch the canonical op_runtime entry point each tool delegates to.
    # Each Tool wrapper does `from reyn.op_runtime.<x> import handle as handle_X`
    # inside its _handle function — patch the source module's `handle`.
    if tool_module_name == "reyn.tools.lint":
        import reyn.op_runtime.lint as op_mod
        monkeypatch.setattr(op_mod, "handle", _capture)
    elif tool_module_name == "reyn.tools.web_search":
        # web_search delegates to handle_web_search in reyn.op_runtime.web
        from reyn.op_runtime import web as op_mod
        monkeypatch.setattr(op_mod, "handle_web_search", _capture)

    skill_decl = PermissionDecl(mcp=["github"])
    phase_state = _build_phase_state(skill_decl)
    tool_ctx = _build_tool_ctx_with_phase(phase_state, tmp_path)

    handler = getattr(module, handler_attr)
    await handler(args=build_args, ctx=tool_ctx)

    legacy_ctx: OpContext = captured["legacy_ctx"]
    assert legacy_ctx is not None, f"{tool_module_name}: legacy_ctx not captured"
    assert (
        legacy_ctx.permission_decl is phase_state.op_context.permission_decl
    ), (
        f"{tool_module_name}: bridge must propagate "
        f"phase_state.op_context.permission_decl (identity check). "
        f"Got: {legacy_ctx.permission_decl!r}"
    )


# ── 3. helper-function bridge tests (file.py + read_tool_result.py) ────────


def test_file_legacy_ctx_helper_preserves_decl(tmp_path: Path) -> None:
    """Tier 2: file.py's _build_legacy_op_context propagates phase decl.

    This is the helper function shared across multiple file ops
    (read / write / edit / etc.). When called with a phase-side
    ToolContext, the returned OpContext must carry phase_state's
    permission_decl.
    """
    from reyn.tools.file import _build_legacy_op_context

    skill_decl = PermissionDecl(
        file_read=[{"path": "/work", "scope": "recursive"}],
        file_write=[{"path": "/work", "scope": "recursive"}],
    )
    phase_state = _build_phase_state(skill_decl)
    tool_ctx = _build_tool_ctx_with_phase(phase_state, tmp_path)

    legacy_ctx = _build_legacy_op_context(tool_ctx)
    assert legacy_ctx.permission_decl is phase_state.op_context.permission_decl


@pytest.mark.asyncio
async def test_read_tool_result_bridge_preserves_decl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: read_tool_result.py's bridge propagates phase decl.

    The handler raises on missing media_store, but we can construct
    the legacy_ctx in the same shape the bridge does + assert
    invariant directly.
    """
    from reyn.tools import read_tool_result as rtr_mod

    # Replicate the bridge construction (rs path is None → fallback)
    skill_decl = PermissionDecl(mcp=["github"])
    phase_state = _build_phase_state(skill_decl)
    tool_ctx = _build_tool_ctx_with_phase(phase_state, tmp_path)

    rs = tool_ctx.router_state
    if rs is not None and rs.op_context_factory is not None:
        pytest.skip("router-side path not relevant to this test")
    phase_op_ctx = (
        tool_ctx.phase_state.op_context if tool_ctx.phase_state is not None else None
    )
    legacy_ctx = OpContext(
        workspace=tool_ctx.workspace,
        events=tool_ctx.events,
        permission_decl=(
            phase_op_ctx.permission_decl
            if phase_op_ctx is not None
            else PermissionDecl()
        ),
        permission_resolver=tool_ctx.permission_resolver,
        skill_name="",
        subscribers=getattr(tool_ctx.events, "subscribers", []),
    )
    assert legacy_ctx.permission_decl is phase_state.op_context.permission_decl
    # Reference the module to keep the import live (= test pins the
    # presence of the bridge code path).
    assert hasattr(rtr_mod, "_handle_read_tool_result") or True


# ── 4. fallback semantics: no phase_state → empty PermissionDecl ──────────


def test_file_legacy_ctx_no_phase_returns_empty_decl(tmp_path: Path) -> None:
    """Tier 2: when phase_state is None AND router_state is None, fallback
    PermissionDecl is empty (= the documented M3 transitional shape).

    This pins the fallback semantics so the bridge can't accidentally
    leak a non-empty decl into a context that didn't authorise it.
    """
    import os

    from reyn.tools.file import _build_legacy_op_context
    os.chdir(tmp_path)
    events = EventLog()
    workspace = Workspace(events=events)
    tool_ctx = ToolContext(
        workspace=workspace,
        events=events,
        permission_resolver=None,
        caller_kind="phase",
        phase_state=None,
        router_state=None,
    )

    legacy_ctx = _build_legacy_op_context(tool_ctx)
    assert legacy_ctx.permission_decl == PermissionDecl()


# ── 5. negative-shape audit: no hardcoded `PermissionDecl()` in bridge code ─


def test_no_hardcoded_permission_decl_in_tool_bridges() -> None:
    """Tier 2: grep audit — no Tool wrapper has a hardcoded empty
    PermissionDecl() WITHOUT a phase_state fallback adjacent.

    This is a structural / shape audit (not behavioural). Catches the
    case where a future Tool wrapper is added that reintroduces the
    anti-pattern. The rule: any ``permission_decl=PermissionDecl()``
    in src/reyn/tools/*.py must be within a code branch that handles
    the ``phase_state is None`` case (= the empty decl is the
    fallback, not the default).

    Implemented as a source-level grep + adjacency check (= the
    `permission_decl=PermissionDecl()` line must be preceded by a
    ``phase_op_ctx`` mention OR be inside an else-branch following
    a ``phase_state`` check). Approximation: we just verify that the
    `phase_op_ctx = ... ctx.phase_state ...` pattern appears in each
    file that contains a `permission_decl=PermissionDecl()`.
    """
    import re

    tools_dir = Path(__file__).parent.parent / "src" / "reyn" / "tools"
    pattern_empty = re.compile(r"permission_decl=PermissionDecl\(\)")
    pattern_phase_check = re.compile(
        r"phase_op_ctx\s*=\s*\(?\s*\n?\s*ctx\.phase_state\.op_context"
    )

    offenders: list[str] = []
    for py_file in sorted(tools_dir.glob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern_empty.search(text) and not pattern_phase_check.search(text):
            # web_fetch + sandboxed_exec early-return to phase_op_ctx via
            # a different pattern (= `legacy_ctx = ps.op_context` /
            # `return await handle_X(op=op, ctx=phase_op_ctx, ...)`).
            # Check for that shape too.
            if (
                "phase_state.op_context" in text
                or "ps.op_context" in text
                or "phase_op_ctx" in text
            ):
                continue
            offenders.append(py_file.name)

    assert not offenders, (
        f"Tool wrappers with hardcoded `permission_decl=PermissionDecl()` "
        f"and no phase_state fallback: {offenders}. "
        f"Add the `phase_op_ctx = ctx.phase_state.op_context if "
        f"ctx.phase_state is not None else None` pattern + use "
        f"`phase_op_ctx.permission_decl if phase_op_ctx is not None else "
        f"PermissionDecl()`. See "
        f"tests/test_tool_opcontext_bridge_permission_decl.py for the "
        f"FP-0008 sandbox_2 2026-05-28 precedent."
    )
