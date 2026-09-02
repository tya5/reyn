"""Tier 2: sandbox-model completion — #1339 structural close.

Pins the wave: (C) single-source default policy resolver; (A) the exec
TOOL (op kind sandboxed_exec) exposes only argv(+timeout) so the LLM cannot
set sandbox axes; (C') the
handler's started event shows the ENFORCED policy network (not the op's request);
(B) both chat OpContext factories resolve a concrete default_sandbox_policy (was
None → the op-fields fallback = the sandbox-escape gap). permission layer
unchanged. No mocks — real Session / adapter / op_runtime handler.
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
from tests._support.agent_session import make_session

# ── (C) single-source resolver ────────────────────────────────────────────────


def test_resolve_returns_default_when_config_none():
    """Tier 2: operator-unset → a concrete default (network=DEFAULT_SANDBOX_NETWORK,
    write_paths tight) — never None, so op-fields are never used.

    #3901 PR-B ④ (owner ruling B): the floor no longer overrides
    ``read_deny_paths`` to the sensitive-path default — that key is absent
    from the floor dict entirely now, so ``SandboxPolicy(**floor)`` falls
    through to the dataclass's own (now-empty) default. An operator who
    wants the old defense-in-depth back sets ``read_deny_paths`` explicitly."""
    pol = resolve_sandbox_policy(None, write_paths=["/ws"])
    assert pol["network"] is DEFAULT_SANDBOX_NETWORK
    assert pol["write_paths"] == ["/ws"]
    assert "read_deny_paths" not in pol


def test_resolve_merges_operator_config_onto_floor():
    """Tier 2: #2964 —an operator's PARTIAL policy is MERGED onto the default
    floor, not substituted wholesale. Only the fields the operator wrote override;
    omitted fields keep the floor (so writing one field never silently drops the
    caller's write_paths)."""
    # operator wrote subprocess only (#3823: config vocabulary, decoupled
    # from the internal SandboxPolicy field name/sense — subprocess=False
    # means denied, translated internally to deny_subprocess=True) —
    # write_paths (caller) must survive (the #2964 silent-drop bug).
    merged = resolve_sandbox_policy({"subprocess": False}, write_paths=["/ws"])
    assert merged["deny_subprocess"] is True          # operator field applied
    assert merged["write_paths"] == ["/ws"]           # caller value SURVIVES (was dropped)
    assert merged["network"] is DEFAULT_SANDBOX_NETWORK


def test_resolve_operator_written_field_overrides_floor():
    """Tier 2: #2964 —a field the operator DID write wins over the floor."""
    merged = resolve_sandbox_policy({"network": False}, write_paths=["/ws"])
    assert merged["network"] is False                 # operator override wins
    assert merged["write_paths"] == ["/ws"]           # unwritten field keeps floor


def test_resolve_explicit_empty_write_paths_is_respected_not_defaulted():
    """Tier 2: #2964 —an operator's EXPLICIT `allow_write_paths: []` is honored
    (the operator deliberately granted nothing), distinct from OMITTING it
    (which keeps the caller's value). dict-key presence expresses the
    explicit-empty-vs-omitted distinction the merge hinges on."""
    explicit_empty = resolve_sandbox_policy({"allow_write_paths": []}, write_paths=["/ws"])
    omitted = resolve_sandbox_policy({"network": False}, write_paths=["/ws"])
    assert explicit_empty["write_paths"] == []        # deliberate empty grant respected
    assert omitted["write_paths"] == ["/ws"]          # omission keeps the floor


# ── (A) tool exposes argv-only ────────────────────────────────────────────────


def test_tool_schema_is_argv_and_timeout_only():
    """Tier 2: #1339 — the exec TOOL exposes only argv (+ timeout, #3903①;
    + collect, #4733) — the LLM cannot set network / fs scope (those stay
    operator-or-default). The pin this test actually protects is THAT
    boundary (no sandbox-policy axis ever reaches the schema), not "the
    key set never grows" — a new key belongs in the EXPECTED set the
    moment it adds a non-axis capability, same as `timeout` did below.

    #3962 dropped `timeout_seconds` from this schema too — same defect
    class as the other removed fields (LLM-advertised, silently ignored on
    the real path), just missed by #3907's own sweep since a wall-clock cap
    isn't a permission axis. #3903① (2026-08-11) brought it back as
    `timeout` — deliberately, with a real reader this time
    (op_runtime/sandboxed_exec.py) — so it belongs in the EXPECTED set now,
    not the removed one; every other axis stays removed.

    #4733: `collect` (enum `["async"]`) selects DISPATCH MODE (sync vs.
    background asyncio.Task on the caller's own session) — orthogonal to
    every axis this test's `removed` loop guards (it sets no network/fs/
    subprocess policy field, and the async path resolves the SAME
    operator-or-default `ctx.default_sandbox_policy` the sync path
    already does, unchanged). It belongs in the EXPECTED set for the same
    reason `timeout` does."""
    from reyn.tools.exec import _EXEC_DESCRIPTION, _EXEC_PARAMETERS

    props = set(_EXEC_PARAMETERS["properties"])
    assert props == {"argv", "timeout", "collect"}
    for removed in (
        "network", "write_paths", "allow_subprocess", "deny_subprocess",
        "env_deny_names", "read_deny_paths", "write_deny_paths",
    ):
        assert removed not in props
    # the description frames the policy as the OPERATOR's (not a settable param)
    assert "operator" in _EXEC_DESCRIPTION.lower()
    # #3903①: no hardcoded number in the static schema text (lead-coder
    # ruling — a per-deployment config value baked into static text would
    # go stale the moment an operator changed it; the authoritative number
    # surfaces via the rejection error instead, see
    # op_runtime/sandboxed_exec.py).
    assert "600" not in _EXEC_DESCRIPTION
    assert "120" not in _EXEC_DESCRIPTION


# ── (C') handler emits the ENFORCED policy network, not the op request ─────────


@pytest.mark.asyncio
async def test_handler_event_shows_enforced_policy_network(tmp_path):
    """Tier 2: #1339 — the started event's network field reflects
    ctx.default_sandbox_policy (the ENFORCED, operator-or-default policy),
    not some other source (a hardcoded default, a raw-dict passthrough
    bypassing SandboxPolicy's own construction, …).

    #3968: this test used to also construct the op with `network=True` to
    demonstrate "the op's own request is overridden" — #3907 deleted that
    field from the op entirely (zero real producers, #3907①'s own
    measurement), so passing it is now a silently-ignored no-op (pydantic
    v2 default) rather than a real request being overridden. The op no
    longer has any lever to request a network posture at all — that half
    of the original claim is structurally covered by #3907's own
    deletion-witness (test_op_sandboxed_exec.py::test_op_no_longer_
    accepts_the_5_deleted_policy_fields), not by constructing an op field
    that no longer exists here. What survives and is unique to THIS test:
    the emitted event actually carries the configured policy value."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.sandboxed_exec import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl
    from tests._support.events import collect_events, settle

    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        default_sandbox_policy={"network": False},  # operator policy: network OFF
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"])
    await handle(op, ctx)
    await settle(events)
    started = [e for e in collected if e.type == "sandboxed_exec_started"]
    (ev,) = started
    assert ev.data["network"] is False  # reflects the configured policy


# ── (B) chat factories resolve a concrete default_sandbox_policy (#1339 root) ──


def test_chat_session_factory_resolves_concrete_policy(tmp_path):
    """Tier 2: #1339 reproduce-first —the Session router OpContext carries a
    concrete default_sandbox_policy (was None → op-fields fallback = the gap)."""
    from reyn.core.events.state_log import StateLog

    session = make_session(
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
    from tests._support.router_host_adapter import make_adapter

    adapter = make_adapter(universal_wrappers_enabled=False)  # #4159: not exercised by this test
    pol = adapter.make_router_op_context().default_sandbox_policy
    assert pol is not None
    assert pol["network"] is DEFAULT_SANDBOX_NETWORK


# ── (D) #3907① — exec.py's minimal-synthesis path also resolves a concrete
# default_sandbox_policy (was None → the same op-fields fallback gap (B) above
# closed, but for the ONE remaining OpContext-building call site that had no
# op_context_factory to delegate to at all) ────────────────────────────────────


@pytest.mark.asyncio
async def test_minimal_synthesis_path_enforces_the_floor_not_the_op_default(
    tmp_path,
) -> None:
    """Tier 2: #3907① — NOT just "the field got filled" (lead-coder's explicit
    correction: filled is not a witness that it's ENFORCED). Drives the REAL
    minimal-synthesis code path (a ToolContext with router_state=None, forcing
    op_context_from_tool_context to skip op_context_factory entirely) through
    the REAL op_runtime handler, and asserts the EMITTED event's enforced
    ``network`` value — mirroring test_handler_event_shows_enforced_policy_network
    above, the same "enforced, not requested" witness shape.

    Before #3907①: ctx.default_sandbox_policy stayed None on this path, so the
    handler fell back to SandboxedExecIROp's own raw field default
    (network=False) — DIFFERENT from every other OpContext-building path in
    the codebase, which resolves through resolve_sandbox_policy's floor
    (network=True, owner decision 2026-06-05). The op itself requests nothing
    (bare argv) — if the fix did nothing, the enforced value would still read
    False (the op-fallback default), indistinguishable from a no-op fill."""
    from reyn.core.events.events import EventLog
    from reyn.data.workspace.workspace import Workspace
    from reyn.tools.exec import op_context_from_tool_context
    from reyn.tools.types import ToolContext
    from tests._support.events import collect_events, settle

    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events, base_dir=tmp_path)
    tool_ctx = ToolContext(
        events=events,
        permission_resolver=None,
        workspace=ws,
        caller_kind="router",
        router_state=None,  # forces the minimal-synthesis branch — no factory to delegate to
    )

    legacy_ctx = await op_context_from_tool_context(tool_ctx)
    assert legacy_ctx.default_sandbox_policy is not None, (
        "ctx.default_sandbox_policy is still None on the minimal-synthesis "
        "path — the fix did not take effect"
    )

    from reyn.core.op_runtime.sandboxed_exec import handle
    from reyn.schemas.models import SandboxedExecIROp

    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "x"])
    await handle(op, legacy_ctx)
    await settle(events)
    started = [e for e in collected if e.type == "sandboxed_exec_started"]
    (ev,) = started
    assert ev.data["network"] is True, (
        "the minimal-synthesis path enforced the op's own raw default "
        "(network=False) instead of the floor (network=True) — the fix did "
        "not change what the handler actually uses, only that a field is set"
    )


@pytest.mark.asyncio
async def test_tool_level_timeout_arg_reaches_the_op(tmp_path):
    """Tier 2: #3903① — the exec TOOL's `timeout` arg (args["timeout"]) is
    threaded by `exec._handle` into `SandboxedExecIROp.timeout_seconds`,
    reaching the real dispatch path — the whole tool→op→handler bridge,
    not just the op-level unit already covered in test_op_sandboxed_exec.py.
    Asserted via the real sandboxed_exec_started audit-event's
    timeout_seconds field (what the handler actually enforced), not a
    private-state read."""
    from reyn.core.events.events import EventLog
    from reyn.data.workspace.workspace import Workspace
    from reyn.tools.exec import _handle
    from reyn.tools.types import ToolContext
    from tests._support.events import collect_events, settle

    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events, base_dir=tmp_path)
    tool_ctx = ToolContext(
        events=events, permission_resolver=None, workspace=ws,
        caller_kind="router", router_state=None,
    )

    await _handle({"argv": ["/bin/echo", "x"], "timeout": 45}, tool_ctx)

    await settle(events)
    (ev,) = [e for e in collected if e.type == "sandboxed_exec_started"]
    assert ev.data["timeout_seconds"] == 45, (
        "the tool-level timeout arg must reach the enforced policy via the "
        f"op bridge, got {ev.data['timeout_seconds']}"
    )
