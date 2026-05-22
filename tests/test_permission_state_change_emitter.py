"""Tier 2: permission_manager → state_change emitter wiring (#398 v4).

The first concrete emitter for the ``notify_state_change`` API
landed in PR #455. When a permission decision is persisted to
``approvals.yaml`` (= user said "yes always" / "no never" on an
intervention), ChatSession mints a ``state_change`` history entry so
the LLM sees the world-state change in its next turn. Directly
mitigates the #352 in-context-learning refusal trap pattern (= the
LLM was continuing to refuse a capability after the user had granted
it, because the permission grant was invisible in the chat history).

Pins:

  1. ``PermissionResolver.register_on_persist`` / ``unregister_on_persist``
     subscriber API exists and stores callbacks in a list.
  2. ``_persist(key, approved)`` fires all registered callbacks with the
     same ``(key, approved)`` args.
  3. ChatSession subscribes itself on construction; the callback fires
     a ``notify_state_change`` with summary derived from ``key`` and
     ``approved``.
  4. Multiple ChatSessions on the same PermissionResolver all receive
     notifications independently (= shared-resolver model).
  5. A bad callback doesn't crash ``_persist`` (= other subscribers
     and the core persistence path are unaffected).
  6. ChatSession.shutdown unregisters the callback so dead-session
     references don't accumulate.

Tier 2 because the contract is load-bearing for #352 closure —
removing the wiring silently re-opens the refusal trap.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.session import ChatMessage, ChatSession
from reyn.events.state_log import StateLog
from reyn.permissions.permissions import PermissionResolver


def _make_session(
    tmp_path: Path, *, agent_name: str, perm_resolver: PermissionResolver,
) -> ChatSession:
    """Build a ChatSession redirected to ``tmp_path`` with a shared
    PermissionResolver injected.
    """
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
        permission_resolver=perm_resolver,
    )


def _make_resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )


def _state_changes(session: ChatSession) -> list[ChatMessage]:
    return [
        m for m in session.history
        if m.role == "system" and (m.meta or {}).get("kind") == "state_change"
    ]


# ── subscriber API on PermissionResolver ───────────────────────────────


def test_register_on_persist_stores_callback(tmp_path):
    """Tier 2: ``register_on_persist`` accepts a callback and stores it.

    Doesn't fire yet — that's a separate test. Pinning the API surface
    so a future refactor that changes the storage shape (= e.g.
    moving to a set / dict) doesn't silently change semantics.
    """
    resolver = _make_resolver(tmp_path)
    called: list = []

    def cb(key, approved):  # noqa: ARG001
        called.append((key, approved))

    resolver.register_on_persist(cb)
    # Internal state: callback list contains it (= use the public
    # API to verify by firing _persist, not by reading private state).
    resolver._persist("test.key", True)
    assert called == [("test.key", True)]


def test_unregister_on_persist_removes_callback(tmp_path):
    """Tier 2: ``unregister_on_persist`` removes a previously registered
    callback so subsequent ``_persist`` invocations don't fire it.

    Returns True on successful removal; False if not found.
    """
    resolver = _make_resolver(tmp_path)
    called: list = []

    def cb(key, approved):  # noqa: ARG001
        called.append(key)

    resolver.register_on_persist(cb)
    assert resolver.unregister_on_persist(cb) is True
    # Second unregister returns False — already removed.
    assert resolver.unregister_on_persist(cb) is False
    resolver._persist("any.key", True)
    assert called == []


def test_persist_fires_all_registered_callbacks(tmp_path):
    """Tier 2: multiple registered callbacks all fire with the same
    ``(key, approved)`` args.
    """
    resolver = _make_resolver(tmp_path)
    cb1_calls = []
    cb2_calls = []
    resolver.register_on_persist(lambda k, a: cb1_calls.append((k, a)))
    resolver.register_on_persist(lambda k, a: cb2_calls.append((k, a)))

    resolver._persist("mcp.sqlite", True)
    resolver._persist("file.write:/path", False)

    assert cb1_calls == [("mcp.sqlite", True), ("file.write:/path", False)]
    assert cb2_calls == [("mcp.sqlite", True), ("file.write:/path", False)]


def test_persist_callback_exception_does_not_crash_persist(tmp_path):
    """Tier 2: a buggy callback (= raises) doesn't crash ``_persist``
    or prevent OTHER subscribers from firing. Defensive isolation —
    observability bugs shouldn't break the core persistence path.
    """
    resolver = _make_resolver(tmp_path)
    good_calls = []

    def bad_cb(key, approved):  # noqa: ARG001
        raise RuntimeError("oops")

    resolver.register_on_persist(bad_cb)
    resolver.register_on_persist(lambda k, a: good_calls.append(k))

    # Must NOT raise.
    resolver._persist("safe.key", True)

    # Good callback still fired despite bad_cb raising.
    assert good_calls == ["safe.key"]
    # And approvals.yaml side still completed.
    assert resolver._saved.get("safe.key") is True


# ── ChatSession wiring ────────────────────────────────────────────────


def test_session_mints_state_change_on_permission_grant(tmp_path):
    """Tier 2 (#398 v4 + #352): a permission grant via
    ``PermissionResolver._persist`` triggers a state_change history
    entry in the subscribed session — the LLM-visible mitigation
    for the #352 in-context-learning refusal trap.

    Summary text follows the documented shape: "Permission for
    '<key>' was granted." (= single-quotes preserve key unambiguity
    when the key contains dots / colons).
    """
    resolver = _make_resolver(tmp_path)
    session = _make_session(tmp_path, agent_name="alpha", perm_resolver=resolver)

    resolver._persist("mcp.sqlite", True)

    entries = _state_changes(session)
    assert len(entries) == 1
    assert entries[0].content == "Permission for 'mcp.sqlite' was granted."
    assert entries[0].meta.get("source") == "permission_manager"


def test_session_mints_state_change_on_permission_revoke(tmp_path):
    """Tier 2: revocation also surfaces — symmetric wording "was
    revoked" so the LLM sees the negative transition too.
    """
    resolver = _make_resolver(tmp_path)
    session = _make_session(tmp_path, agent_name="alpha", perm_resolver=resolver)

    resolver._persist("mcp.sqlite", False)

    entries = _state_changes(session)
    assert len(entries) == 1
    assert entries[0].content == "Permission for 'mcp.sqlite' was revoked."
    assert entries[0].meta.get("source") == "permission_manager"


def test_multiple_sessions_all_notified_on_shared_resolver(tmp_path):
    """Tier 2: when N ChatSessions share the same PermissionResolver
    (= the typical reyn web / reyn run model), a single permission
    grant notifies all N sessions independently. Each gets its own
    state_change history entry — the grant is project-wide, all
    agents in the project should know.
    """
    resolver = _make_resolver(tmp_path)
    session_a = _make_session(tmp_path, agent_name="alpha", perm_resolver=resolver)
    session_b = _make_session(tmp_path, agent_name="beta", perm_resolver=resolver)

    resolver._persist("file.write:/data", True)

    assert len(_state_changes(session_a)) == 1
    assert len(_state_changes(session_b)) == 1


def test_session_with_no_resolver_works_unchanged(tmp_path):
    """Tier 2: ChatSession can be constructed without a
    PermissionResolver (= CLI / test stubs). No callback registered,
    no crash on shutdown. Backward compat path.
    """
    session = ChatSession(
        agent_name="loner",
        state_log=StateLog(tmp_path / "loner.wal"),
        snapshot_path=tmp_path / "loner_snapshot.json",
        # permission_resolver=None — default
    )
    assert session._on_perm_persist_cb is None
    # No exception even though no resolver was wired.


@pytest.mark.asyncio
async def test_session_shutdown_unregisters_callback(tmp_path):
    """Tier 2: ``ChatSession.shutdown`` unregisters its callback from
    the shared PermissionResolver — prevents dead-session references
    from accumulating across long-running ``reyn web`` sessions.
    """
    resolver = _make_resolver(tmp_path)
    session = _make_session(tmp_path, agent_name="ephemeral", perm_resolver=resolver)

    pre = len(resolver._on_persist_callbacks)
    assert pre == 1

    await session.shutdown()
    # Drain the shutdown signal that shutdown put on the inbox so the
    # call doesn't block other tests if reused. Inbox shutdown isn't
    # the focus here — just confirming unregister fired.
    assert session._on_perm_persist_cb is None
    assert len(resolver._on_persist_callbacks) == pre - 1
