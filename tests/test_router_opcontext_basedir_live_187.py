"""Tier 2: #187 — the LIVE router OpContext factory roots file ops on the container repo.

Follow-up to #1410, which patched the WRONG method. The registry file-op dispatch
uses ``op_context_factory = getattr(host, "make_router_op_context")``
(router_loop.py), and the chat host is ``RouterHostAdapter`` — so the LIVE factory
is ``RouterHostAdapter.make_router_op_context``. #1410 instead patched the parallel
``ChatSession._make_router_op_context`` (used only by the legacy ``_file_op`` /
``_mcp_call_tool`` callbacks); the two impls had drifted, and #1410's test was green
on the legacy seam while live file ops still ran on the host cwd (astropy 0/6).

This test exercises the LIVE factory (``session._router_host.make_router_op_context``
— the exact method the dispatch resolves) so a regression to host-cwd on the live
path fails here. (Lesson: verify the live dispatch impl, not a parallel seam.)
"""
from __future__ import annotations

from pathlib import Path

from reyn.chat.session import ChatSession
from reyn.environment.container_backend import DockerEnvironmentBackend


def test_live_op_context_roots_on_container_repo(tmp_path) -> None:
    """Tier 2: with a docker env-backend + container base_dir, the LIVE op-context
    factory (RouterHostAdapter.make_router_op_context) builds a Workspace rooted on
    the container repo (/testbed) over the docker backend — so file__read/grep/glob/
    edit resolve in-container, not on the host reyn cwd."""
    backend = DockerEnvironmentBackend(container="c1", repo_dir="/testbed")
    s = ChatSession(
        agent_name="t", environment_backend=backend,
        workspace_base_dir=Path("/testbed"), workspace_state_dir=tmp_path,
    )
    # THE live factory: router_loop dispatch uses op_context_factory =
    # getattr(host, "make_router_op_context"); host is this adapter.
    ctx = s._router_host.make_router_op_context()
    assert ctx.workspace.base_dir == Path("/testbed"), (
        "live router file ops must root on the container repo, not the host cwd "
        "(the #1410 miss: this LIVE factory still defaulted to cwd)"
    )
    # the exec sandbox write_paths derive from workspace.base_dir → container-scoped too
    assert "/testbed" in str(ctx.default_sandbox_policy)


def test_live_op_context_host_default_unchanged() -> None:
    """Tier 2: no env-backend / base_dir (host backend / interactive chat) → the live
    factory keeps the host cwd default (the fix only takes effect under a container
    base_dir)."""
    s = ChatSession(agent_name="t")
    ctx = s._router_host.make_router_op_context()
    assert ctx.workspace.base_dir == Path.cwd()
