"""Tier 2: S3.1c-2 — SandboxLayer ∩ wired into the http gate (network only).

#1199 S3.1c-2 originally completed the conjunctive-∩ model for
``require_file_read/write`` AND ``require_http_get`` by folding the resolved
sandbox policy into a SandboxLayer ∩. #3901 PR-B ③ retired FILE_READ /
FILE_WRITE from SandboxLayer's permission-∩ projection (an operator cannot
know a sandbox's path floor, so it is no longer treated as permission — see
``effective.py``'s own ``SandboxLayer`` docstring) — the file-gate tests this
module used to carry were removed accordingly (test_offload_read_grant_1383.py
carries the surviving "sandbox_policy no longer narrows file reads" witness).

The http gate's NETWORK_HOST veto is UNCHANGED by ③ (lead-coder ruling,
#3901 thread) and remains covered below — network is a value an operator
writes directly into ``reyn.yaml sandbox.policy``, not a workspace floor they
cannot know, so it stays in the permission ∩ (the same shape as
SUBPROCESS/ENV, not the same shape as the path caps ③ retired). An earlier
draft of ③ grouped NETWORK_HOST with the two path axes and briefly broke this
veto — see ``effective.py``'s ``SandboxLayer`` docstring for why it stayed.

ProfileLayer is intentionally NOT wired into these gates: it constrains only
skill / mcp (⊤ for file / network), so it would be a provably dead layer.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.security.permissions.permissions import PermissionDecl
from reyn.security.sandbox.policy import SandboxPolicy
from tests._support.permissions import make_resolver as _make_resolver

# ── http gate: SandboxLayer ∩ (network) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_http_sandbox_network_false_denies(tmp_path: Path) -> None:
    """Tier 2: a sandbox with network:false vetoes http_get (no bus needed —
    denial precedes the prompt path)."""
    r = _make_resolver(tmp_path)
    decl = PermissionDecl(http_get=[{"host": "*"}])
    with pytest.raises(PermissionError, match="sandbox"):
        await r.require_http_get(
            decl, "api.x.com", None, "s", sandbox_policy=SandboxPolicy(network=False),
        )


@pytest.mark.asyncio
async def test_http_sandbox_bypass_prevention_config_allow(tmp_path: Path) -> None:
    """Tier 2: ★bypass-prevention — a CONFIG-ALLOWED host is STILL DENIED when the
    sandbox disables network. The veto sits BEFORE the allow tiers; if it were
    placed after config-allow this would wrongly pass — so the placement is
    load-bearing (sandbox RESTRICT overrides AgentLayer config GRANT)."""
    r = _make_resolver(tmp_path, config={"web.fetch": "allow"})  # blanket config-allow
    with pytest.raises(PermissionError, match="sandbox"):
        await r.require_http_get(
            PermissionDecl(), "api.x.com", None, "s",
            sandbox_policy=SandboxPolicy(network=False),
        )


@pytest.mark.asyncio
async def test_http_sandbox_network_true_does_not_veto(tmp_path: Path) -> None:
    """Tier 2: network:true sandbox + config-allow → passes (sandbox does not veto;
    the config grant stands)."""
    r = _make_resolver(tmp_path, config={"web.fetch": "allow"})
    await r.require_http_get(
        PermissionDecl(), "api.x.com", None, "s",
        sandbox_policy=SandboxPolicy(network=True),
    )  # no raise


# ── caller-split: the op-handler helper ──────────────────────────────────────


def test_sandbox_policy_from_ctx_builds_and_none() -> None:
    """Tier 2: the op-handler helper builds a SandboxPolicy from the phase dict and
    returns None when unset (the caller-split: phase handlers thread the policy,
    OS-internal callers get None = SandboxLayer ⊤)."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext, sandbox_policy_from_ctx
    from reyn.data.workspace.workspace import Workspace

    events = EventLog()
    ws = Workspace(events)
    with_policy = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        default_sandbox_policy={"network": True, "write_paths": ["/x"]},
    )
    policy = sandbox_policy_from_ctx(with_policy)
    assert policy is not None
    assert policy.network is True
    assert policy.write_paths == ["/x"]

    without = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
    )
    assert sandbox_policy_from_ctx(without) is None
