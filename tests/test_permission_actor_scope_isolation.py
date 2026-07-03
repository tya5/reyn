"""Tier 2: OS invariant — approval keys are actor-scoped (privilege isolation).

A path approval granted under one actor MUST NOT be honored for a different
actor. The approval key's first segment (``{actor}/{kind}/{path}``) is the
privilege-isolation boundary that prevents an external actor from inheriting
another actor's grants.

This is the enforcement-equivalence guard for the ``skill_name``→``actor``
rename: only the identifier changed — the actor VALUE still scopes every
approval, so a grant never leaks across actors. The test goes RED if the actor
segment is dropped from the key (the rename would then collapse the isolation).

Real PermissionResolver, no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.security.permissions.permissions import PermissionResolver

# The actor categories the field carries in production. Non-empty actors
# (session-router, router, tool-verb name) scope approvals; the empty actor is
# unscoped and can never hold a grant (see test_empty_actor_is_never_approved).
_GRANTABLE_ACTORS = ["chat_router", "router", "drop_source"]


def _resolver(project_root: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False
    )


def _out_of_zone(tmp_path: Path) -> tuple[PermissionResolver, str]:
    """A resolver plus a write path OUTSIDE the default write zone (needs approval)."""
    proj = (tmp_path / "proj").resolve()
    proj.mkdir(parents=True)
    outside = (tmp_path / "state" / "secret.json").resolve()
    outside.parent.mkdir(parents=True)
    return _resolver(proj), str(outside)


@pytest.mark.parametrize("granted_actor", _GRANTABLE_ACTORS)
def test_write_grant_does_not_leak_across_actors(tmp_path: Path, granted_actor: str) -> None:
    """Tier 2: a write approval granted for one actor is honored for THAT actor
    only — every other actor (and the unscoped empty actor) is still denied
    (no cross-actor privilege leak)."""
    r, path = _out_of_zone(tmp_path)

    # Before any grant, the out-of-zone write is denied for every actor.
    assert r.is_write_allowed(path, granted_actor) is False

    # Grant the write for one actor.
    r.session_approve_path(path, actor=granted_actor, kind="file.write")

    # The granted actor is now allowed…
    assert r.is_write_allowed(path, granted_actor) is True

    # …but no OTHER actor inherits the grant (the isolation invariant).
    for other in _GRANTABLE_ACTORS + [""]:
        if other == granted_actor:
            continue
        assert r.is_write_allowed(path, other) is False, (
            f"grant for {granted_actor!r} leaked to {other!r} — actor isolation broken"
        )


def test_empty_actor_is_never_approved(tmp_path: Path) -> None:
    """Tier 2: the empty (unscoped) actor cannot hold an approval — even an
    explicit grant under "" leaves the out-of-zone write denied, so an
    unscoped caller never gains persisted privileges."""
    r, path = _out_of_zone(tmp_path)
    r.session_approve_path(path, actor="", kind="file.write")
    assert r.is_write_allowed(path, "") is False


def test_recursive_grant_is_actor_scoped(tmp_path: Path) -> None:
    """Tier 2: a recursive (directory) grant under one actor covers children for
    that actor only — a different actor is denied a child path."""
    r, _ = _out_of_zone(tmp_path)
    base = (tmp_path / "state" / "dir").resolve()
    base.mkdir(parents=True)
    child = str(base / "nested" / "file.json")

    r.session_approve_path(str(base), actor="chat_router", kind="file.write", recursive=True)

    assert r.is_write_allowed(child, "chat_router") is True
    assert r.is_write_allowed(child, "drop_source") is False
