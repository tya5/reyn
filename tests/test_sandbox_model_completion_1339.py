"""Tier 2: sandbox-model completion — #1339 structural close.

Pins the wave: (C) single-source default policy resolver; (A) the sandboxed_exec
TOOL exposes only argv(+timeout) so the LLM cannot set sandbox axes; (C') the
handler's started event shows the ENFORCED policy network (not the op's request);
(B) both chat OpContext factories resolve a concrete default_sandbox_policy (was
None → the op-fields fallback = the sandbox-escape gap). permission layer
unchanged. No mocks — real ChatSession / adapter / op_runtime handler.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from reyn.security.sandbox.policy import (  # noqa: E402
    DEFAULT_SANDBOX_NETWORK,
    resolve_sandbox_policy,
)

# ── (C) single-source resolver ────────────────────────────────────────────────


def test_resolve_returns_default_when_config_none():
    """Tier 2: operator-unset → a concrete default (network=DEFAULT_SANDBOX_NETWORK,
    write_paths tight, sensitive deny-list) — never None, so op-fields are never used."""
    pol = resolve_sandbox_policy(None, write_paths=["/ws"])
    assert pol["network"] is DEFAULT_SANDBOX_NETWORK
    assert pol["write_paths"] == ["/ws"]
    assert "~/.ssh" in pol["read_deny_paths"]


def test_resolve_returns_operator_config_verbatim():
    """Tier 2: an operator-declared policy is returned as-is (operator owns it)."""
    cfg = {"network": False, "write_paths": ["/only/here"]}
    assert resolve_sandbox_policy(cfg, write_paths=["/ignored"]) == cfg


# ── (A) tool exposes argv-only ────────────────────────────────────────────────


def test_tool_schema_is_argv_only():
    """Tier 2: #1339 —the sandboxed_exec TOOL exposes only argv + timeout — the
    LLM cannot set network / fs scope (those are operator-or-default)."""
    from reyn.tools.sandboxed_exec import (
        _SANDBOXED_EXEC_DESCRIPTION,
        _SANDBOXED_EXEC_PARAMETERS,
    )

    props = set(_SANDBOXED_EXEC_PARAMETERS["properties"])
    assert props == {"argv", "timeout_seconds"}
    for removed in ("network", "read_paths", "write_paths", "allow_subprocess"):
        assert removed not in props
    # the description frames the policy as the OPERATOR's (not a settable param)
    assert "operator" in _SANDBOXED_EXEC_DESCRIPTION.lower()


# ── (C') handler emits the ENFORCED policy network, not the op request ─────────


@pytest.mark.asyncio
async def test_handler_event_shows_enforced_policy_network(tmp_path):
    """Tier 2: #1339 —op requests network=True but the ctx policy is network=False;
    the started event must report the ENFORCED value (False), and the run must
    actually use the policy (not the op's request)."""
    from reyn.events.events import EventLog
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.sandboxed_exec import handle
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.workspace.workspace import Workspace

    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        default_sandbox_policy={"network": False},  # operator policy: network OFF
    )
    op = SandboxedExecIROp(
        kind="sandboxed_exec", argv=["/bin/echo", "x"], network=True,  # op REQUESTS network
    )
    await handle(op, ctx, caller="control_ir")
    started = [e for e in events.all() if e.type == "sandboxed_exec_started"]
    (ev,) = started
    assert ev.data["network"] is False  # enforced policy, NOT op.network=True


# ── (B) chat factories resolve a concrete default_sandbox_policy (#1339 root) ──


def test_chat_session_factory_resolves_concrete_policy(tmp_path):
    """Tier 2: #1339 reproduce-first —the ChatSession router OpContext carries a
    concrete default_sandbox_policy (was None → op-fields fallback = the gap)."""
    from reyn.chat.session import ChatSession
    from reyn.events.state_log import StateLog

    session = ChatSession(
        agent_name="b",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    pol = session._make_router_op_context().default_sandbox_policy
    assert pol is not None
    assert pol["network"] is DEFAULT_SANDBOX_NETWORK


def test_router_adapter_factory_resolves_concrete_policy():
    """Tier 2: #1339 reproduce-first —the RouterHostAdapter router OpContext also
    carries a concrete default_sandbox_policy (wire-full-path — both factories)."""
    from test_router_host_adapter_invariants import _make_adapter

    adapter = _make_adapter()
    pol = adapter.make_router_op_context().default_sandbox_policy
    assert pol is not None
    assert pol["network"] is DEFAULT_SANDBOX_NETWORK
