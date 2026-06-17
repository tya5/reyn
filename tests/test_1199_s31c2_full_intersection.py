"""Tier 2: S3.1c-2 — SandboxLayer ∩ wired into the file + http gates (full-∩).

#1199 S3.1c-2 completes the conjunctive-∩ model: ``require_file_read/write`` and
``require_http_get`` now fold the resolved sandbox policy
(``ctx.default_sandbox_policy`` — agent-level since #1326) into a SandboxLayer ∩.
A sandbox path/network cap RESTRICTS even an AgentLayer-granted
(zone / config-approved) path — restrict-only, the sandbox cannot grant. None (the
OS's own in-process ops / non-sandboxed callers) → SandboxLayer ⊤ (unchanged).

ProfileLayer is intentionally NOT wired into these gates: it constrains only
skill / mcp (⊤ for file / network), so it would be a provably dead layer.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.security.sandbox.policy import SandboxPolicy
from tests.test_permissions import _make_resolver

# ── file gates: SandboxLayer ∩ (path caps) ───────────────────────────────────


@pytest.mark.asyncio
async def test_sandbox_cap_restricts_in_zone_write(tmp_path: Path) -> None:
    """Tier 2: a sandbox write_paths cap DENIES even an in-zone write — the ∩
    narrows (sandbox restrict-only over the AgentLayer zone grant)."""
    r = _make_resolver(tmp_path)
    # .reyn/x is in the default write zone (AgentLayer grants) but the sandbox
    # caps writes to /sandboxed → ∩ denies.
    policy = SandboxPolicy(write_paths=["/sandboxed"])
    with pytest.raises(PermissionError):
        await r.require_file_write(PermissionDecl(), ".reyn/x.txt", "s", sandbox_policy=policy)


@pytest.mark.asyncio
async def test_sandbox_allow_all_permits(tmp_path: Path) -> None:
    """Tier 2: write_paths=['/'] (= swe_bench's allow-all policy) → in-zone write
    passes (the no-current-blast-radius case)."""
    r = _make_resolver(tmp_path)
    await r.require_file_write(
        PermissionDecl(), ".reyn/x.txt", "s",
        sandbox_policy=SandboxPolicy(write_paths=["/"]),
    )  # no raise


@pytest.mark.asyncio
async def test_no_sandbox_policy_unchanged(tmp_path: Path) -> None:
    """Tier 2: sandbox_policy=None → SandboxLayer ⊤ — the gate behaves exactly as
    pre-S3.1c-2 (in-zone write passes; non-sandboxed callers unaffected)."""
    r = _make_resolver(tmp_path)
    await r.require_file_write(PermissionDecl(), ".reyn/x.txt", "s")  # no raise


@pytest.mark.asyncio
async def test_sandbox_falsification_removing_layer_regrants(tmp_path: Path) -> None:
    """Tier 2: ★∩-falsification — the SAME in-zone write the sandbox DENIES is
    ALLOWED once the SandboxLayer is dropped (sandbox_policy=None). Proves the
    SandboxLayer is load-bearing (over-grant if removed)."""
    r = _make_resolver(tmp_path)
    path = ".reyn/x.txt"
    capped = SandboxPolicy(write_paths=["/sandboxed"])  # does not cover .reyn/
    with pytest.raises(PermissionError):
        await r.require_file_write(PermissionDecl(), path, "s", sandbox_policy=capped)
    # drop the SandboxLayer → re-granted by the zone baseline (the over-grant).
    await r.require_file_write(PermissionDecl(), path, "s", sandbox_policy=None)  # no raise


@pytest.mark.asyncio
async def test_sandbox_read_cap_restricts(tmp_path: Path) -> None:
    """Tier 2: the read gate folds the SandboxLayer too (read_paths cap)."""
    r = _make_resolver(tmp_path)
    capped = SandboxPolicy(read_paths=["/sandboxed"])  # excludes cwd
    with pytest.raises(PermissionError):
        await r.require_file_read(PermissionDecl(), "src/reyn/x.py", "s", sandbox_policy=capped)


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
