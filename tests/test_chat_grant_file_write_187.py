"""Tier 2: OS invariant — `reyn chat --grant-file-write` resolver ∩ sandbox grant.

#187: solving SWE with the general agent (RouterLoop / `reyn chat`) in a
non-interactive / scripted container run needs the agent to edit the repo
working tree without a permission prompt. `reyn chat` gained a scoped
`--grant-file-write` flag, symmetric with `reyn run` (test_run_grant_file_write_183).

Unlike `reyn run` (where the skill declares `file.read`), a chat agent has NO
skill, so the chat flag grants BOTH `file.read` AND `file.write` (the eval
swe_bench path, eval_benchmark.py:742, does the same). The SandboxLayer (∩)
bounds the write to the env-backend's `write_paths` (= the repo working tree), so
the blanket resolver `allow` is scoped to the working tree, NOT global.

Pins with a REAL PermissionResolver + real SandboxPolicy (never a None resolver,
per the enforcement-test rule):
  - chat grant injects file.read AND file.write = 'allow';
  - grant + sandbox[repo] → an in-repo write is ALLOWED;
  - the SAME grant → a write OUTSIDE the sandbox zone is DENIED (∩ scopes it);
  - WITHOUT the grant → an in-repo write is DENIED (the prompt-less default);
  - `reyn chat` exposes `--grant-file-write` (dest=grant_file_write).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.sandbox.policy import SandboxPolicy

_REPO = "/testbed"
_DECL = PermissionDecl()


def _chat_grant_config(*, granted: bool) -> dict:
    """Mirror what `reyn chat --grant-file-write` injects into config_permissions
    (chat.py): setdefault file.read AND file.write to 'allow'."""
    config: dict = {}
    if granted:
        config.setdefault("file.read", "allow")
        config.setdefault("file.write", "allow")
    return config


def _resolver(*, granted: bool) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=_chat_grant_config(granted=granted),
        project_root=Path("/tmp"),
        interactive=False,
    )


async def _can_write(resolver: PermissionResolver, path: str) -> bool:
    sandbox = SandboxPolicy(write_paths=[_REPO])
    try:
        await resolver.require_file_write(_DECL, path, "default", sandbox_policy=sandbox)
        return True
    except PermissionError:
        return False


def test_chat_grant_injects_read_and_write() -> None:
    """Tier 2: the chat grant injects BOTH file.read and file.write (no skill
    declares read for a chat agent, unlike `reyn run`)."""
    config = _chat_grant_config(granted=True)
    assert config.get("file.read") == "allow"
    assert config.get("file.write") == "allow"


@pytest.mark.asyncio
async def test_chat_grant_allows_in_repo_write() -> None:
    """Tier 2: chat grant + sandbox[repo] → an in-repo write is allowed (agent edits)."""
    assert await _can_write(_resolver(granted=True), f"{_REPO}/astropy/io/ascii/html.py") is True


@pytest.mark.asyncio
async def test_chat_grant_still_denies_outside_sandbox_zone() -> None:
    """Tier 2: the SandboxLayer ∩ scopes the chat grant — a write OUTSIDE write_paths
    (e.g. /etc) is DENIED even with the resolver grant (scope from the sandbox, not
    the blanket resolver allow). Same safety as `reyn run --grant-file-write`."""
    assert await _can_write(_resolver(granted=True), "/etc/passwd") is False


@pytest.mark.asyncio
async def test_chat_no_grant_denies_in_repo_write() -> None:
    """Tier 2: flag absent → no grant → even an in-repo write is DENIED (the
    non-interactive prompt-less default). Falsification pair for the grant test."""
    assert await _can_write(_resolver(granted=False), f"{_REPO}/astropy/io/ascii/html.py") is False


def test_chat_parser_exposes_grant_file_write_flag() -> None:
    """Tier 2: `reyn chat` registers --grant-file-write (dest=grant_file_write),
    default False — symmetric with `reyn run`."""
    from reyn.cli.commands.chat import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register(sub)
    assert parser.parse_args(["chat", "--grant-file-write"]).grant_file_write is True
    assert parser.parse_args(["chat"]).grant_file_write is False
