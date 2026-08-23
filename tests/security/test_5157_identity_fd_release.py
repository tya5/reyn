"""Tier 2: #5157 — the #5152 identity fd has a real release trigger beyond
a same-key rebind, and its in-use count is observable.

Root cause (e2e-coder, #5152's own TESTS-READY(B), test-review Q5 — "what
does this accumulate, and who bounds it?"): #5152's ``_acquire_identity_fd``
held an fd open per bound key for the remaining life of the process, with a
same-key rebind as the ONLY release path.

Architect ruling (issuecomment-5383671820): the population here is
rate-limited by a HUMAN, not machine-driven growth — one fd per distinct
PATH-flavor approval, and an approval is only ever created by a person
granting it. An LRU cap was explicitly REJECTED: making eviction routine
would demote a still-protected key into the "cannot confirm" bucket as a
matter of course, letting pool SIZE (not the approval's own lifecycle)
decide the security guarantee's scope. The actual gap closed here: a human
REVOKING an approval (``_persist(key, False)``) now also releases that key's
fd — the same #5146-style "settle on disappearance" idiom, this time
triggered by revocation rather than a purge — and the in-use count is now a
public read (``bound_fd_count``) so the population assumption is checkable
without reaching into private state.

Real ``PermissionResolver`` + real filesystem paths — no mocks, same harness
convention as ``test_5042_approval_identity_binding.py``.
"""
from __future__ import annotations

import pytest

from reyn.security.permissions import PermissionDecl
from reyn.security.permissions.permissions import PermissionResolver


def _make_resolver(tmp_path) -> PermissionResolver:
    return PermissionResolver({}, project_root=tmp_path)


@pytest.mark.asyncio
async def test_bound_fd_count_reflects_distinct_bound_paths(tmp_path) -> None:
    """Tier 2: #5157 acceptance ② — the in-use fd count is a public read,
    and it counts distinct BOUND path-flavor keys, not distinct calls."""
    resolver = _make_resolver(tmp_path)
    assert resolver.bound_fd_count() == 0

    for i in range(3):
        d = tmp_path / f"dir{i}"
        d.mkdir()
        key = f"actor/file.write/dir{i}/"
        resolver._saved[key] = True
        await resolver.require_file_write(PermissionDecl(), str(d / "f.txt"), "actor")

    assert resolver.bound_fd_count() == 3

    # A SECOND use of an already-bound key does not grow the count.
    await resolver.require_file_write(
        PermissionDecl(), str((tmp_path / "dir0") / "g.txt"), "actor",
    )
    assert resolver.bound_fd_count() == 3


@pytest.mark.asyncio
async def test_revoking_an_approval_releases_its_identity_fd(tmp_path) -> None:
    """Tier 2: #5157 acceptance ① — a human revoking a path approval
    (``_persist(key, False)``) is the disappearance-trigger for its
    identity fd: nothing left to protect once the grant itself is gone.
    This is the actual gap the issue named ("a rebind was the ONLY
    release path"); revocation is now a second one."""
    resolver = _make_resolver(tmp_path)
    target = tmp_path / "some_dir"
    target.mkdir()
    key = "actor/file.write/some_dir/"
    resolver._saved[key] = True

    await resolver.require_file_write(
        PermissionDecl(), str(target / "f.txt"), "actor",
    )
    assert resolver.bound_fd_count() == 1

    resolver._persist(key, False)  # the revoke surface (web/CLI) calls this
    assert resolver.bound_fd_count() == 0, (
        "revoking the approval must release its identity fd, not leak it"
    )


@pytest.mark.asyncio
async def test_revoking_one_key_does_not_release_a_different_keys_fd(
    tmp_path,
) -> None:
    """Tier 2: #5157 — the release is scoped to the revoked KEY only; an
    unrelated bound key's fd survives untouched."""
    resolver = _make_resolver(tmp_path)

    kept_dir = tmp_path / "kept_dir"
    kept_dir.mkdir()
    kept_key = "actor/file.write/kept_dir/"
    resolver._saved[kept_key] = True
    await resolver.require_file_write(
        PermissionDecl(), str(kept_dir / "f.txt"), "actor",
    )

    revoked_dir = tmp_path / "revoked_dir"
    revoked_dir.mkdir()
    revoked_key = "actor/file.write/revoked_dir/"
    resolver._saved[revoked_key] = True
    await resolver.require_file_write(
        PermissionDecl(), str(revoked_dir / "f.txt"), "actor",
    )
    assert resolver.bound_fd_count() == 2

    resolver._persist(revoked_key, False)
    assert resolver.bound_fd_count() == 1
