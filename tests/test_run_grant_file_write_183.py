"""Tier 2: OS invariant — `reyn run --grant-file-write` resolver ∩ sandbox grant.

#183: a non-interactive `reyn run` leaves a skill's declared ``file.write`` as
"declared-but-not-granted", so the apply phase cannot edit the working tree
(astropy-13453 aborted: "outside the allowed write zone"). The
``--grant-file-write`` flag grants ``file.write`` at the resolver (AgentLayer),
and the SandboxLayer (∩) bounds it to the run's ``write_paths`` (= the repo
working tree) — so the effective grant is scoped to the working tree, NOT global.

These pin the security-critical ∩ with a REAL PermissionResolver + real
SandboxPolicy (never a None resolver, per the enforcement-test rule):
  - grant + sandbox[repo] → a write under the repo is ALLOWED;
  - the SAME grant → a write OUTSIDE the sandbox zone (e.g. /etc) is DENIED
    (the SandboxLayer ∩ does the scoping, not the resolver grant);
  - WITHOUT the grant (opt-out / flag absent) → even an in-repo write is DENIED
    (the declared-but-not-granted state = the #183 bug the flag fixes);
  - the `reyn run` arg parser exposes `--grant-file-write` (dest=grant_file_write).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.security.sandbox.policy import SandboxPolicy

_REPO = "/testbed"
_DECL = PermissionDecl()


def _resolver(*, granted: bool) -> PermissionResolver:
    """A REAL resolver (non-interactive). ``granted`` mirrors what
    --grant-file-write injects: config_permissions['file.write'] = 'allow'."""
    config = {"file.write": "allow"} if granted else {}
    return PermissionResolver(
        config_permissions=config,
        project_root=Path("/tmp"),
        interactive=False,
    )


async def _can_write(resolver: PermissionResolver, path: str) -> bool:
    sandbox = SandboxPolicy(write_paths=[_REPO])
    try:
        await resolver.require_file_write(_DECL, path, "swe_bench", sandbox_policy=sandbox)
        return True
    except PermissionError:
        return False


@pytest.mark.asyncio
async def test_grant_allows_in_repo_write() -> None:
    """Tier 2: grant + sandbox[repo] → an in-repo write is allowed (apply can edit)."""
    assert await _can_write(_resolver(granted=True), f"{_REPO}/astropy/io/ascii/html.py") is True


@pytest.mark.asyncio
async def test_grant_still_denies_outside_sandbox_zone() -> None:
    """Tier 2: the SandboxLayer ∩ scopes the grant — a write OUTSIDE write_paths
    (e.g. /etc) is DENIED even with the resolver grant. This is the scoping that
    makes the blanket resolver-`allow` safe: scope comes from the sandbox, not a
    (non-functional) scoped resolver config."""
    assert await _can_write(_resolver(granted=True), "/etc/passwd") is False


@pytest.mark.asyncio
async def test_no_grant_denies_in_repo_write() -> None:
    """Tier 2: opt-out (flag absent → no grant) → even an in-repo write is DENIED.

    This is the #183 bug the flag fixes: a declared-but-not-granted file.write in
    a non-interactive run. Falsification pair for the grant test above."""
    assert await _can_write(_resolver(granted=False), f"{_REPO}/astropy/io/ascii/html.py") is False


def test_run_parser_exposes_grant_file_write_flag() -> None:
    """Tier 2: `reyn run` registers --grant-file-write (dest=grant_file_write),
    default False — the wiring that toggles the grant injection."""
    from reyn.interfaces.cli.commands.run import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register(sub)
    # NB: no trailing positional — a stray positional after the skill name is
    # rejected by Python 3.11 argparse (3.12 tolerates intermixed positionals).
    assert parser.parse_args(["run", "swe_bench", "--grant-file-write"]).grant_file_write is True
    assert parser.parse_args(["run", "swe_bench"]).grant_file_write is False
