"""Tier 2: ``index_query`` / ``index_drop`` handlers
raise a clear ValueError when ``ctx.workspace`` is None.
(#1303 Stage I deleted the ``index_write`` op.)

Pinned invariant:

- Each of the 3 index op handlers checks ``ctx.workspace is None`` BEFORE
  attempting ``ctx.workspace.base_dir`` access.
- On None, the handler raises ``ValueError`` with an actionable error
  message that names (a) the op kind, (b) the cause (= router-side path
  with no workspace), (c) the fix path (= pass an OpContext with
  workspace).
- The opaque ``AttributeError: 'NoneType' object has no attribute
  'base_dir'`` is no longer surfaced to control_ir_failed events.

Motivation: B48 W2-S7 (= chained_find_then_index) showed 4 consecutive
``control_ir_failed`` events with the opaque AttributeError. The
``ctx.workspace`` was None because the calling tool (e.g. recall)
propagated a workspace-less ToolContext through. The opaque error
prevented the LLM and operators from understanding the actual cause.
This guard makes the failure mode actionable.

testing.ja.md compliance:
- No mocks. Real ``handle()`` called against a real OpContext with
  ``workspace=None``.
- Tier 2 contract pin on the documented error shape.
- No private-state assertions.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.index_drop import handle as index_drop_handle
from reyn.core.op_runtime.index_query import handle as index_query_handle
from reyn.schemas.models import IndexDropIROp, IndexQueryIROp
from reyn.security.permissions.permissions import PermissionDecl


class _NullEventLog:
    """Minimal stand-in for EventLog. The handlers raise before emitting,
    so no behavior is needed beyond the constructor."""

    def __init__(self):
        self.subscribers = []

    def emit(self, *args, **kwargs):
        pass


def _make_ctx_no_workspace() -> OpContext:
    """Construct an OpContext with workspace=None — the failure mode the
    fix guards against."""
    return OpContext(
        workspace=None,
        events=_NullEventLog(),
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        skill_name="",
    )


# ---------------------------------------------------------------------------
# index_query
# ---------------------------------------------------------------------------


def test_index_query_raises_clear_value_error_when_workspace_none():
    """Tier 2: index_query handler must raise a clear ``ValueError``
    (not opaque ``AttributeError``) when workspace is None.
    (B48-NF-W2-S7 fix)"""
    op = IndexQueryIROp(
        kind="index_query",
        source="test_source",
        query="test",
        top_k=5,
        query_vector=[0.1, 0.2, 0.3],  # non-None to bypass fallback
    )
    ctx = _make_ctx_no_workspace()

    with pytest.raises(ValueError, match="index_query") as exc_info:
        asyncio.run(index_query_handle(op, ctx, caller="control_ir"))

    msg = str(exc_info.value)
    assert "workspace" in msg.lower()
    # actionable hint: should mention OpContext + how to fix
    assert "OpContext" in msg
    assert "router-side" in msg.lower()


def test_index_query_fallback_path_bypasses_workspace_check():
    """Tier 2: when ``query_vector is None`` the function returns the
    fallback enumerate result WITHOUT touching workspace — the workspace
    guard must not fire on the fallback path. Regression guard against
    over-eager guard placement."""
    op = IndexQueryIROp(
        kind="index_query",
        source="test_source",
        query="test",
        top_k=5,
        query_vector=None,  # triggers fallback
    )
    ctx = _make_ctx_no_workspace()

    # No exception, returns the fallback empty result.
    result = asyncio.run(index_query_handle(op, ctx, caller="control_ir"))
    assert result == {"chunks": [], "mode": "fallback"}


# ---------------------------------------------------------------------------
# index_drop
# ---------------------------------------------------------------------------


def test_index_drop_raises_clear_value_error_when_workspace_none():
    """Tier 2: index_drop handler raises a clear ``ValueError``
    when workspace is None. (B48-NF-W2-S7 fix)"""
    op = IndexDropIROp(kind="index_drop", source="test_source")
    ctx = _make_ctx_no_workspace()

    with pytest.raises(ValueError, match="index_drop") as exc_info:
        asyncio.run(index_drop_handle(op, ctx, caller="control_ir"))

    msg = str(exc_info.value)
    assert "workspace" in msg.lower()
    assert "OpContext" in msg


# ---------------------------------------------------------------------------
# Regression guard: AttributeError no longer surfaces
# ---------------------------------------------------------------------------
# (#1303 Stage I deleted the index_write op; index_query + index_drop remain.)


def test_no_attribute_error_on_workspace_none_for_any_index_op():
    """Tier 2: across the index handlers, the opaque
    ``AttributeError: 'NoneType' object has no attribute 'base_dir'``
    must NOT be raised — the new ValueError guard takes precedence."""
    ctx = _make_ctx_no_workspace()

    # index_query
    op_q = IndexQueryIROp(
        kind="index_query", source="s", query="q", top_k=1,
        query_vector=[0.1],
    )
    with pytest.raises(ValueError):
        asyncio.run(index_query_handle(op_q, ctx, caller="control_ir"))
    # And NOT AttributeError
    try:
        asyncio.run(index_query_handle(op_q, ctx, caller="control_ir"))
    except ValueError:
        pass
    except AttributeError as e:
        pytest.fail(
            f"index_query should not raise AttributeError on None workspace; "
            f"got: {e}"
        )

    # index_drop
    op_d = IndexDropIROp(kind="index_drop", source="s")
    try:
        asyncio.run(index_drop_handle(op_d, ctx, caller="control_ir"))
    except ValueError:
        pass
    except AttributeError as e:
        pytest.fail(
            f"index_drop should not raise AttributeError on None workspace; "
            f"got: {e}"
        )
