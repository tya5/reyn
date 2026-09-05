"""Tier 2: #5820 -- describe_session's write_scope no longer lies about a
declared sandbox.policy (owner-hit, silent).

Real Session/RouterCallerState/OpContext throughout -- no mocks. Root
cause (tui-coder's own throwaway reproduction, confirmed by lead-coder):
``tools/exec.py``'s ``op_context_from_tool_context`` bridge used to derive
a policy-less ``SandboxConfig(backend=name)`` from ``RouterCallerState.
sandbox_backend`` (a NAME string) and OVERWRITE ``legacy_ctx.
sandbox_config`` wholesale with it via ``_with_sandbox_config`` -- even
though ``legacy_ctx.sandbox_config`` (from a real ``op_context_factory()``)
already carried the operator's REAL declared ``sandbox.policy``.
``describe_session``'s ``write_scope`` field (``describe_write_scope``,
``runtime/session_write_scope.py``) reads exactly that field, so it
started reporting ``{"declared": False}`` for every session with a real
(non-None) sandbox backend, regardless of what the operator actually
declared -- a false negative, never an exception, so nobody saw a red.

Fix (lead-coder ruling: (a), use the EXISTING seam): the backend NAME is
resolved to a real ``SandboxBackend`` instance and set on
``OpContext.sandbox_backend`` instead -- the field whose own docstring
already names "an injected instance wins over name-based auto-selection"
as its purpose. ``sandbox_config`` is never touched post-construction any
more; ``_with_sandbox_config`` (the one rewriter) is deleted outright.
"""
from __future__ import annotations

import pytest

from reyn.config import SandboxConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.session_write_scope import describe_write_scope
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.tools.exec import op_context_from_tool_context
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session


def _real_session_ctx(tmp_path, *, declared_write_paths):
    """A real Session + real op-context-factory path, mirroring
    ``test_5818_router_op_context_strict_mode_wiring.py``'s own
    established pattern in this directory."""
    session = make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        sandbox_config=SandboxConfig(
            mode="strict", backend="noop", policy={"allow_write_paths": declared_write_paths},
        ),
    )
    return session._make_router_op_context()


@pytest.mark.asyncio
async def test_bridge_preserves_the_declared_policy_through_a_real_backend(tmp_path) -> None:
    """Tier 2: the concrete #5820 witness -- a session with a REAL declared
    ``allow_write_paths`` AND a real (non-None) sandbox backend name. After
    the bridge runs, ``sandbox_config.policy`` (what ``write_scope`` reads)
    must still carry that declaration -- not silently replaced with a
    policy-less stand-in."""
    real_ctx = _real_session_ctx(tmp_path, declared_write_paths=["/declared/path"])
    assert real_ctx.sandbox_config is not None
    assert real_ctx.sandbox_config.policy == {"allow_write_paths": ["/declared/path"]}

    rs = RouterCallerState(op_context_factory=lambda: real_ctx, sandbox_backend="noop")
    tool_ctx = ToolContext(
        events=real_ctx.events, permission_resolver=real_ctx.permission_resolver,
        workspace=real_ctx.workspace, caller_kind="router", router_state=rs,
    )
    bridged_ctx = await op_context_from_tool_context(tool_ctx)

    # The declared policy survives -- the actual #5820 regression pin.
    assert bridged_ctx.sandbox_config is not None
    assert bridged_ctx.sandbox_config.policy == {"allow_write_paths": ["/declared/path"]}
    assert describe_write_scope(bridged_ctx.sandbox_config) == {
        "declared": True,
        "allow_write_paths": ["/declared/path"],
        "deny_write_paths": None,
    }

    # Enforcement is unaffected -- was already correct, stays correct.
    assert bridged_ctx.default_sandbox_policy["write_paths"] == ["/declared/path"]


@pytest.mark.asyncio
async def test_bridge_gives_the_backend_name_its_own_seam_not_sandbox_config(tmp_path) -> None:
    """Tier 2: driven verification (not a static "same table" claim) that
    resolving the backend NAME early, in the bridge, produces the SAME
    concrete backend a caller resolving it later (at exec time, off
    ``OpContext.sandbox_backend`` directly, per that field's own "an
    injected instance wins" contract) would get -- and that
    ``sandboxed_exec.py``'s own real ``resolve_backend`` call actually
    uses the injected instance rather than re-resolving."""
    from reyn.security.sandbox.launcher import resolve_backend

    real_ctx = _real_session_ctx(tmp_path, declared_write_paths=[])
    rs = RouterCallerState(op_context_factory=lambda: real_ctx, sandbox_backend="noop")
    tool_ctx = ToolContext(
        events=real_ctx.events, permission_resolver=real_ctx.permission_resolver,
        workspace=real_ctx.workspace, caller_kind="router", router_state=rs,
    )
    bridged_ctx = await op_context_from_tool_context(tool_ctx)

    assert isinstance(bridged_ctx.sandbox_backend, NoopBackend)
    # sandboxed_exec.py's own exact call shape (op_runtime/sandboxed_exec.py:99)
    # — the injected instance short-circuits `or`, never re-resolving.
    resolved = resolve_backend(bridged_ctx.sandbox_backend, bridged_ctx.sandbox_config)
    assert resolved is bridged_ctx.sandbox_backend


@pytest.mark.asyncio
async def test_bridge_leaves_sandbox_config_object_identity_unchanged(tmp_path) -> None:
    """Tier 2: the bridge must not even reconstruct an equal-but-different
    ``sandbox_config`` object -- ``is``, not ``==``, is the witness that no
    rewrite path exists any more (a subtler regression than a VALUE change:
    a future "helpfully" re-wrapping ``sandbox_config`` in a new object
    with the same fields would pass a value-equality check but still be a
    rewrite path reopening this exact class)."""
    real_ctx = _real_session_ctx(tmp_path, declared_write_paths=["/x"])
    rs = RouterCallerState(op_context_factory=lambda: real_ctx, sandbox_backend="noop")
    tool_ctx = ToolContext(
        events=real_ctx.events, permission_resolver=real_ctx.permission_resolver,
        workspace=real_ctx.workspace, caller_kind="router", router_state=rs,
    )
    bridged_ctx = await op_context_from_tool_context(tool_ctx)
    assert bridged_ctx.sandbox_config is real_ctx.sandbox_config


def test_no_source_file_rewrites_op_context_sandbox_config() -> None:
    """Tier 1: structural absence, src-wide AST (mirrors ``tests/repo/
    test_router_op_context_single_source_1412.py``'s own established
    pattern for "this class of drift must be impossible, not merely
    avoided"). A FRESH ``OpContext(...)`` construction legitimately sets
    ``sandbox_config`` once, from its own real source (``build_router_op_
    context``, ``tools/exec.py``'s own minimal-synthesis branch) — that is
    not a rewrite. What must never exist: a ``dataclasses.replace(<existing
    ctx>, sandbox_config=...)`` call, which takes an ALREADY-BUILT context
    (whose ``sandbox_config`` may already carry a real declared policy) and
    swaps that ONE field for something else — ``_with_sandbox_config``'s
    own #5820 root cause, exactly this shape. A reintroduced instance of it
    anywhere in ``src/reyn`` fails this, naming file:line."""
    import ast

    from tests._support.paths import REPO_ROOT

    src = REPO_ROOT / "src" / "reyn"
    offenders: list[str] = []
    for py in sorted(src.rglob("*.py")):
        rel = str(py.relative_to(src))
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_replace = (
                isinstance(node.func, ast.Attribute) and node.func.attr == "replace"
            ) or (isinstance(node.func, ast.Name) and node.func.id == "replace")
            if not is_replace:
                continue
            for kw in node.keywords:
                if kw.arg == "sandbox_config":
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "a dataclasses.replace(...) site passes sandbox_config= -- this "
        "reopens #5820's own class ('display source rewritable "
        f"independent of enforcement source'): {offenders}"
    )


def test_dataclasses_replace_import_still_present_where_expected() -> None:
    """Tier 1: a plain sanity pin — dataclasses.replace is still importable
    the way this module's own AST-scan test above assumes (``import
    dataclasses`` + ``dataclasses.replace``, the attribute form), so that
    scan's own "is_replace" detection has a real positive case to prove it
    is not vacuously green over zero matches anywhere in the tree."""
    import inspect

    import reyn.tools.exec as exec_module

    src_text = inspect.getsource(exec_module)
    assert "dataclasses.replace(" in src_text, (
        "no dataclasses.replace( call found in tools/exec.py at all — the "
        "AST scan's own is_replace branch would never be exercised by "
        "this file, making its own green a vacuous one"
    )
    # And that ONE call site must not carry sandbox_config= any more.
    assert "dataclasses.replace(legacy_ctx, sandbox_config=" not in src_text
