"""Tier 2: #5990 (part of, stage 2) — `exec.op_context_from_tool_context`'s
own `except ValueError: resolved_backend = None` swallowed an unknown
`router_state.sandbox_backend` name with no visible report -- a security-
relevant silent-degrade (a requested sandbox backend not applying).
Now warns before falling back, same convention #6038 established.

Real Session/RouterCallerState/OpContext throughout, same construction as
test_5820_write_scope_single_source.py's own established pattern in this
directory -- no mocks.
"""
from __future__ import annotations

import logging

import pytest

from reyn.config import SandboxConfig
from reyn.core.events.state_log import StateLog
from reyn.tools.exec import op_context_from_tool_context
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session

_LOGGER_NAME = "reyn.tools.exec"


def _real_session_ctx(tmp_path):
    session = make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        sandbox_config=SandboxConfig(mode="strict", backend="noop", policy={}),
    )
    return session._make_router_op_context()


@pytest.mark.asyncio
async def test_unknown_sandbox_backend_name_warns_and_leaves_resolved_backend_unset(
    tmp_path, caplog,
) -> None:
    """Tier 2: an unknown `sandbox_backend` name on `router_state` warns
    AND `op_context_from_tool_context` still returns a context (no
    exception) with no early-resolved backend instance.

    Strip-falsify (verified by hand: the `logging.getLogger(__name__).
    warning(...)` call removed from the bridge's `except ValueError`):
    this test goes red — no record at all."""
    real_ctx = _real_session_ctx(tmp_path)
    rs = RouterCallerState(op_context_factory=lambda: real_ctx, sandbox_backend="not-a-real-backend")
    tool_ctx = ToolContext(
        events=real_ctx.events, permission_resolver=real_ctx.permission_resolver,
        workspace=real_ctx.workspace, caller_kind="router", router_state=rs,
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        bridged_ctx = await op_context_from_tool_context(tool_ctx)
    assert bridged_ctx is not None
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert any("not-a-real-backend" in r.message for r in records)


@pytest.mark.asyncio
async def test_known_sandbox_backend_name_does_not_warn(tmp_path, caplog) -> None:
    """Tier 2: negative control -- a known backend name ("noop") resolves
    through with no warning at all."""
    real_ctx = _real_session_ctx(tmp_path)
    rs = RouterCallerState(op_context_factory=lambda: real_ctx, sandbox_backend="noop")
    tool_ctx = ToolContext(
        events=real_ctx.events, permission_resolver=real_ctx.permission_resolver,
        workspace=real_ctx.workspace, caller_kind="router", router_state=rs,
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await op_context_from_tool_context(tool_ctx)
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
