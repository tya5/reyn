"""Tier 2: #5052 — a saved ``http.get`` approval carries a ``scope`` (who it
applies to), closing the reported leak: ``_is_host_approved_for``'s key
(``"{actor}/{kind}/{host}"``) has NO agent dimension at all, so an approval
granted while running as one agent silently applied to EVERY agent in the
workspace.

Design ruling (architect "C", adopted by lead-coder, issue #5052 thread):
scope is a VALUE the approval entry carries, never a position in the key —
``agent:<name>`` (default) or ``workspace`` (explicit, wide). A third value,
``session:<sid>``, was explicitly rejected (a session id CAN be reused —
``registry.py``'s ``_has_session`` only tracks currently-live sessions, and
nothing records a retired one — so a session-scoped grant would silently
reattach to an unrelated later session).

Migration choice for a PRE-#5052 record (no ``scope`` field at all — the
ledger is append-only, so such a record can never be rewritten to carry
one): it is read as ``SCOPE_LEGACY_WORKSPACE`` — STILL HONORED workspace-
wide (that was genuinely its meaning the moment it was granted; there was
no agent dimension for the operator to have narrowed), NOT silently
promoted to the same value as an explicit ``SCOPE_WORKSPACE`` choice, and
NOT silently unreadable either. The moment the same key is re-approved,
the newest record (which DOES carry an explicit scope) wins the fold.

Real ``PermissionResolver`` + a real ``ApprovalLedger`` on a real on-disk
``.reyn/`` tree throughout — no mocks. A fresh ``PermissionResolver``
instance per "agent" call forces each read to actually fold the ledger
from disk, the same way two independent agent processes sharing one
project would each see it.

Strip-falsifier for ``test_default_agent_scoped_approval_does_not_leak_to_
a_second_agent``: remove the ``self._scope_covers_agent(key, agent_name)``
check from ``PermissionResolver._is_host_approved_for`` (revert to
``return bool(self._saved.get(key) or self._session.get(key))``) — the
test goes RED (agent B's prompt fires ``bus_b.requests == []`` and no
``PermissionError`` is raised, i.e. agent B's request is silently
approved by agent A's grant). Verified for real in this PR's own commit
history: committed with the check in place (green), the check removed
(red), then reverted (green again) — not merely asserted here.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.security.permissions.approval_ledger import (
    SCOPE_LEGACY_WORKSPACE,
    SCOPE_WORKSPACE,
    ApprovalLedger,
    scope_for_agent,
)
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

_HOST = "example.com"
_ACTOR = "chat_router"
_KEY = f"{_ACTOR}/http.get/{_HOST}"


class _ChoiceBus(InterventionBus):
    """A real (non-mock) ``InterventionBus`` that always answers with one
    fixed choice — same shape as ``test_5236_permission_approval_granted_
    audit_event.py``'s own ``_ChoiceBus``."""

    def __init__(self, choice_id: str) -> None:
        self.requests: "list[UserIntervention]" = []
        self._choice_id = choice_id

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id=self._choice_id)


def _ledger(project_root: Path) -> ApprovalLedger:
    return ApprovalLedger(project_root / ".reyn" / "approvals.jsonl")


def _decl() -> PermissionDecl:
    return PermissionDecl(http_get=[{"host": "*"}])


# ── (a) default agent-scoped approval does not leak to a second agent ───────


def test_default_agent_scoped_approval_does_not_leak_to_a_second_agent(
    tmp_path: Path,
) -> None:
    """Tier 2: Agent A grants ALWAYS (default scope = ``agent:agent-a``) -> agent B,
    a DIFFERENT agent sharing the SAME project/ledger, is asked again
    rather than silently inheriting A's grant.

    Six-questions #4 caveat, stated explicitly: a config with only ONE
    default agent name (e.g. calling ``require_http_get`` twice with the
    SAME ``agent_name``) would pass this assertion trivially even under
    the OLD buggy no-agent-dimension key, because the second call would
    simply find the cached grant and return -- that is not what this test
    does. Distinguishing "scoped correctly" from "just cached" requires
    TWO DISTINCT agent names, which is what makes this a real 2-agent
    witness rather than a single-agent one that happens to look like it.
    """
    bus_a = _ChoiceBus("always")
    resolver_a = PermissionResolver(config_permissions={}, project_root=tmp_path)
    asyncio.run(
        resolver_a.require_http_get(
            _decl(), _HOST, bus_a, _ACTOR, agent_name="agent-a",
        )
    )
    assert bus_a.requests, "control arm: agent A's prompt must have fired at all"

    # A FRESH resolver instance for agent B -- forces a real fold of the
    # SAME on-disk ledger agent A just wrote to (mirrors two independent
    # agent processes sharing one project).
    bus_b = _ChoiceBus("no")
    resolver_b = PermissionResolver(config_permissions={}, project_root=tmp_path)
    with pytest.raises(PermissionError):
        asyncio.run(
            resolver_b.require_http_get(
                _decl(), _HOST, bus_b, _ACTOR, agent_name="agent-b",
            )
        )
    assert bus_b.requests, (
        "agent B must have been prompted independently -- if this list is "
        "empty, agent A's grant was silently honored for agent B (the "
        "#5052 leak)."
    )

    # Ledger-level corroboration: the scope actually recorded is agent-a's,
    # not workspace-wide. #5431: read via a fresh `ApprovalLedger.fold()`
    # (the same production surface `reyn permissions list` / `GET
    # /api/permissions` use) rather than the removed `saved_scope_get`
    # accessor — a strictly fresher read than that cached accessor ever
    # was, not a weaker one.
    _saved, _bound, scopes = _ledger(tmp_path).fold()
    assert scopes[_KEY] == scope_for_agent("agent-a")


# ── (b) an explicit workspace-scoped approval DOES apply to both agents ────


def test_explicit_workspace_scope_applies_to_both_agents(tmp_path: Path) -> None:
    """Tier 2: a grant explicitly recorded with ``scope=SCOPE_WORKSPACE`` (the
    operator's deliberate wide choice, never the silent default) is
    honored for EVERY agent -- the flip side of (a): scope narrows by
    DEFAULT, not unconditionally."""
    _ledger(tmp_path).append_approval(_KEY, True, SCOPE_WORKSPACE)

    for agent_name in ("agent-a", "agent-b"):
        bus = _ChoiceBus("no")  # must NEVER be consulted -- already approved
        resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
        asyncio.run(
            resolver.require_http_get(_decl(), _HOST, bus, _ACTOR, agent_name=agent_name)
        )
        assert bus.requests == [], (
            f"agent {agent_name!r} should not have been prompted -- a "
            f"workspace-scoped grant covers every agent"
        )


# ── (c) a legacy (pre-#5052, scope-less) entry: the documented migration ───


def test_legacy_scopeless_entry_is_still_honored_workspace_wide(
    tmp_path: Path,
) -> None:
    """Tier 2: a record with NO ``scope`` field at all (the shape every approval
    written before this fix has, and the shape the append-only ledger can
    NEVER rewrite to add one) is treated as ``SCOPE_LEGACY_WORKSPACE`` --
    still granted, for every agent, exactly as it was the moment it was
    approved. This is the chosen migration behavior (documented in
    ``approval_ledger.py``'s own module docstring): NOT silently promoted
    to the SAME value as an explicit ``workspace`` choice (a #4996-family
    "unspecified" vs "specified-and-wide" distinction), and NOT silently
    treated as unreadable/gone -- both forbidden shapes lead-coder named."""
    _ledger(tmp_path).append_approval(_KEY, True, scope=None)  # pre-#5052 shape

    for agent_name in ("agent-a", "agent-b"):
        bus = _ChoiceBus("no")  # must NEVER be consulted -- already approved
        resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
        asyncio.run(
            resolver.require_http_get(_decl(), _HOST, bus, _ACTOR, agent_name=agent_name)
        )
        assert bus.requests == [], (
            f"agent {agent_name!r} should not have been prompted -- a "
            f"legacy scope-less grant still covers every agent"
        )
        # #5431: fresh ledger fold, same reasoning as above.
        _saved, _bound, scopes = _ledger(tmp_path).fold()
        assert scopes[_KEY] == SCOPE_LEGACY_WORKSPACE, (
            "a scope-less record must classify as the LEGACY sentinel, "
            "never silently collapsed into SCOPE_WORKSPACE"
        )


def test_legacy_scopeless_entry_is_visible_not_silent_on_the_listing_surface(
    tmp_path: Path,
) -> None:
    """Tier 2: architect's explicit 'not silently' requirement: a legacy entry
    still in effect must be OBSERVABLE, not merely functional. The web
    router's own ``_load`` (the real production read path behind
    ``GET /api/permissions``) reports this key's scope as the legacy
    sentinel -- a caller (or an operator-facing listing) can count it,
    rather than it disappearing into an indistinguishable "workspace"
    label."""
    _ledger(tmp_path).append_approval(_KEY, True, scope=None)

    from reyn.interfaces.web.routers.permissions import _load

    approvals, scopes = _load(tmp_path)
    assert approvals[_KEY] is True
    assert scopes[_KEY] == SCOPE_LEGACY_WORKSPACE
    legacy_count = sum(
        1 for k, v in approvals.items() if v and scopes.get(k) == SCOPE_LEGACY_WORKSPACE
    )
    assert legacy_count == 1, "the legacy grant must be countable, not silent"


# ── (d) strip-falsifier control: the fold-level scope classification itself ─


def test_fold_classifies_absent_scope_as_a_different_value_than_workspace(
    tmp_path: Path,
) -> None:
    """Tier 2: direct ``ApprovalLedger.fold()`` witness (no ``PermissionResolver``
    involved) for the #4996-family distinction this design depends on:
    an EXPLICIT ``workspace`` record and a scope-LESS record must fold to
    two DIFFERENT scope strings, even though both currently match every
    agent at lookup time. Collapsing them into the same string would be
    exactly the "unspecified promoted to specified-and-wide" shape the
    architect ruling forbids."""
    ledger = _ledger(tmp_path)
    ledger.append_approval("a/http.get/x.example.com", True, SCOPE_WORKSPACE)
    ledger.append_approval("b/http.get/y.example.com", True, scope=None)
    _approvals, _bound, scopes = ledger.fold()
    assert scopes["a/http.get/x.example.com"] == SCOPE_WORKSPACE
    assert scopes["b/http.get/y.example.com"] == SCOPE_LEGACY_WORKSPACE
    assert scopes["a/http.get/x.example.com"] != scopes["b/http.get/y.example.com"]
