"""Tier 2: OS invariant — `reyn chat --grant-file-write` resolver grant.

#187: solving SWE with the general agent (RouterLoop / `reyn chat`) in a
non-interactive / scripted container run needs the agent to edit the repo
working tree without a permission prompt. `reyn chat` gained a scoped
`--grant-file-write` flag, symmetric with `reyn run` (test_run_grant_file_write_183).

Unlike `reyn run` (where the skill declares `file.read`), a chat agent has NO
skill, so the chat flag grants BOTH `file.read` AND `file.write` (the eval
swe_bench path, eval_benchmark.py:742, does the same).

#3901 PR-B ③ (owner ruling B, #3916): FILE_WRITE no longer participates in
SandboxLayer's permission-∩ projection — an operator cannot know a sandbox's
path floor, so it is not treated as a permission input. This grant has no
scope of its own at the permission layer; scope is a #3925 concern on the
permission side (not yet built), and any sandbox-backend enforcement (e.g.
Landlock/seatbelt denying a write outside its own configured paths) is a
SEPARATE, backend-level mechanism this test does not exercise. Do not write
"the sandbox narrows it" here again — that conflation is exactly what PR-B
retired.

Pins with a REAL PermissionResolver (never a None resolver, per the
enforcement-test rule):
  - chat grant injects file.read AND file.write = 'allow';
  - grant → a write is ALLOWED (no scope check at this layer);
  - WITHOUT the grant → the same write is DENIED (the prompt-less default);
  - `reyn chat` exposes `--grant-file-write` (dest=grant_file_write).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.security.sandbox.policy import SandboxPolicy

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
    # A real sandbox_policy is threaded through, same as a live caller would —
    # proving the grant's outcome does NOT depend on it (FILE_WRITE is ⊤ on
    # SandboxLayer post-PR-B ③, not merely untested here).
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
    """Tier 2: chat grant → an in-repo write is allowed (agent edits)."""
    assert await _can_write(_resolver(granted=True), f"{_REPO}/astropy/io/ascii/html.py") is True


@pytest.mark.asyncio
async def test_chat_no_grant_denies_in_repo_write() -> None:
    """Tier 2: flag absent → no grant → even an in-repo write is DENIED (the
    non-interactive prompt-less default). Falsification pair for the grant test."""
    assert await _can_write(_resolver(granted=False), f"{_REPO}/astropy/io/ascii/html.py") is False


def test_chat_parser_exposes_grant_file_write_flag() -> None:
    """Tier 2: `reyn chat` registers --grant-file-write (dest=grant_file_write),
    default False — symmetric with `reyn run`."""
    from reyn.interfaces.cli.commands.chat import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register(sub)
    assert parser.parse_args(["chat", "--grant-file-write"]).grant_file_write is True
    assert parser.parse_args(["chat"]).grant_file_write is False
