"""Tier 2: #6140 — the no-declaration ``http.get`` legacy-compat path in
``require_http_get`` recorded an ALWAYS answer under the bare ``KEY_WEB_
FETCH`` constant (no host in the key at all), so ONE "Allow fetching from
{host!r}?" answer silently authorised EVERY future host — including hosts
a DIFFERENT actor's own DECLARED wildcard would otherwise have prompted
for individually (the bare-key short-circuit fired before that per-host
prompt ever ran).

Real ``PermissionResolver`` + a real interactive-choice ``InterventionBus``
throughout (the ``_ChoiceBus`` shape ``test_5052_approval_scope_dimension.
py`` / ``test_5236_permission_approval_granted_audit_event.py`` already
establish) — no mocks. Each of the 3 issue-body acceptance criteria gets
its own test, plus the full #6140 -> #6141 -> #6142 -> #6146 arc:

- #6140/#6141: the legacy path stops WRITING new bare-key grants —
  indexed per-host instead, same shape a DECLARED grant uses.
- #6141 round 2 / #6142: the bare-key READ (still needed for backward
  compat with an ALREADY-persisted grant) is narrowed to fire only for
  an UNDECLARED host — a DECLARED host (specific or wildcard) can never
  be silently covered by it.
- #6146 (this file's own current state): the bare-key READ is REMOVED
  ENTIRELY. Every undeclared host now always reaches the real
  interactive prompt (or raises, with no bus) — a pre-#6140 grant
  already on disk, or a `require_web_fetch`-tool ALWAYS answer, no
  longer authorises anything in `require_http_get`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.security.permissions.approval_ledger import ApprovalLedger
from reyn.security.permissions.permissions import (
    KEY_WEB_FETCH,
    PermissionDecl,
    PermissionResolver,
)
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

_ACTOR = "skill"


class _ChoiceBus(InterventionBus):
    """A real (non-mock) ``InterventionBus`` that always answers with one
    fixed choice — same shape ``test_5052_approval_scope_dimension.py``'s
    own ``_ChoiceBus``."""

    def __init__(self, choice_id: str) -> None:
        self.requests: "list[UserIntervention]" = []
        self._choice_id = choice_id

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id=self._choice_id)


def _resolver(tmp_path: Path, *, config: "dict | None" = None) -> PermissionResolver:
    return PermissionResolver(config_permissions=config or {}, project_root=tmp_path)


# ── ① undeclared A approved -> undeclared B is asked again ──────────────


def test_an_undeclared_host_approved_does_not_silently_cover_a_second_undeclared_host(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- issue #6140's own acceptance criterion 1.

    FALSIFY: reverting the fix (indexing the legacy-path approval under
    the bare `KEY_WEB_FETCH` constant again) makes this go red -- host
    B's own bus is never consulted (`bus_b.requests == []`), because the
    `:1947` bare-key short-circuit finds A's grant first."""
    bus_a = _ChoiceBus("always")
    resolver_a = _resolver(tmp_path)
    asyncio.run(
        resolver_a.require_http_get(PermissionDecl(), "a.example.com", bus_a, _ACTOR)
    )
    assert bus_a.requests, "control arm: host A's prompt must have fired at all"

    # A fresh resolver -- forces a real fold of the SAME on-disk ledger.
    bus_b = _ChoiceBus("no")
    resolver_b = _resolver(tmp_path)
    with pytest.raises(PermissionError, match="denied"):
        asyncio.run(
            resolver_b.require_http_get(PermissionDecl(), "b.example.com", bus_b, _ACTOR)
        )
    assert bus_b.requests, (
        "host B must have been prompted independently -- an empty list "
        "means host A's undeclared grant silently covered host B too "
        "(the #6140 defect)."
    )


# ── ② undeclared A approved -> a DECLARED wildcard host C is still asked
#      (the breadth witness the issue body names explicitly) ────────────


def test_an_undeclared_host_approved_does_not_short_circuit_a_declared_wildcard_host(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- issue #6140's own acceptance criterion 2, the
    explicit "breadth" witness: an actor with a DECLARED wildcard
    ``http.get`` decl must still be prompted per-host, even after a
    (different, undeclared-path) grant was recorded for this same actor.

    FALSIFY: reverting the fix makes this go red -- `bus_c.requests ==
    []`, because `:1947`'s bare-`KEY_WEB_FETCH` short-circuit returns
    before the declared per-host prompt (the membership-routed path)
    ever runs."""
    bus_a = _ChoiceBus("always")
    resolver_a = _resolver(tmp_path)
    asyncio.run(
        resolver_a.require_http_get(PermissionDecl(), "a.example.com", bus_a, _ACTOR)
    )
    assert bus_a.requests

    bus_c = _ChoiceBus("no")
    resolver_c = _resolver(tmp_path)
    wildcard_decl = PermissionDecl(http_get=[{"host": "*"}])
    with pytest.raises(PermissionError, match="denied"):
        asyncio.run(
            resolver_c.require_http_get(wildcard_decl, "c.example.com", bus_c, _ACTOR)
        )
    assert bus_c.requests, (
        "the declared-wildcard host C must still be prompted per-host -- "
        "an empty list means the undeclared-path grant for host A short-"
        "circuited it (the #6140 defect, breadth witness)."
    )


# ── ③ `web.fetch: allow` config behavior stays unchanged ────────────────


def test_web_fetch_allow_config_still_pre_approves_without_prompting(
    tmp_path: Path,
) -> None:
    """Tier 2: deny -- issue #6140's own acceptance criterion 3 (§6:
    "unchanged"). The `web.fetch: allow` CONFIG-tier blanket approval
    (`_is_config_approved(KEY_WEB_FETCH)`, untouched by this fix) must
    still pre-approve every host with no prompt at all -- this fix only
    changed what a RUNTIME (session/saved) grant indexes by, never the
    config tier."""
    bus = _ChoiceBus("no")  # must NEVER be consulted
    resolver = _resolver(tmp_path, config={KEY_WEB_FETCH: "allow"})
    asyncio.run(
        resolver.require_http_get(PermissionDecl(), "anything.example.com", bus, _ACTOR)
    )
    assert bus.requests == [], (
        "web.fetch: allow must pre-approve without any prompt -- this "
        "config-tier behavior must stay byte-identical to before #6140."
    )


# ── the fix's own mechanism: per-host indexing ───────────────────────────


def test_the_legacy_grant_is_indexed_per_host_not_under_the_bare_key(
    tmp_path: Path,
) -> None:
    """Tier 2: the load-bearing mechanism witness -- the SAME key shape
    `_is_host_approved_for` already reads for a DECLARED per-host grant
    (`<actor>/http.get/<host>`) is what the undeclared legacy path now
    writes to, read back here via a real `ApprovalLedger.fold()` (the
    same production surface `reyn permissions list` uses)."""
    from reyn.security.permissions.approval_ledger import ApprovalLedger

    bus = _ChoiceBus("always")
    resolver = _resolver(tmp_path)
    asyncio.run(
        resolver.require_http_get(PermissionDecl(), "example.com", bus, _ACTOR)
    )

    saved, _bound, _scopes = ApprovalLedger(
        tmp_path / ".reyn" / "approvals.jsonl"
    ).fold()
    assert saved.get(f"{_ACTOR}/http.get/example.com") is True
    assert KEY_WEB_FETCH not in saved, (
        f"expected no NEW entry under the bare {KEY_WEB_FETCH!r} key -- "
        f"got {saved!r}"
    )


# ── #6146: the bare-key READ is gone -- neither remaining holder shape
#      authorises an undeclared host any more ─────────────────────────


def test_a_pre_6140_blanket_grant_no_longer_authorises_an_undeclared_host(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- one branch of #6146's own acceptance criterion
    ⑴: witnesses, via ``bus=None`` (the narrow fallback route #6150
    already guards -- NOT a real session's own non-interactive path),
    that the removed bare-key read is genuinely gone -- the shortest
    possible witness that ``PermissionError`` is now reached at all.
    A REAL session's non-interactive route (no listener -> bus IS
    present -> ``AuditOnlyInterventionBridge``'s own typed refusal ->
    ``_approve()`` returns ``False`` -> ``PermissionError("... denied.")``)
    is OUTSIDE what this test covers -- the sibling test below,
    ``..._no_longer_short_circuits_the_interactive_prompt`` (a real
    bus, ``bus.requests`` non-empty), is the live-route witness. A real
    pre-#6140-shaped ledger entry (the bare key, no host) is written
    directly here -- the fixed code path can no longer produce this
    shape at all -- to reproduce exactly what an operator's already-
    existing approvals.yaml looks like.

    FALSIFY: restoring the removed bare-key short-circuit (checking
    ``self._saved.get(KEY_WEB_FETCH)`` and returning early) makes this
    go red -- no ``PermissionError`` is raised, the call returns
    silently instead."""
    ApprovalLedger(tmp_path / ".reyn" / "approvals.jsonl").append_approval(
        KEY_WEB_FETCH, True, "workspace",
    )

    resolver = _resolver(tmp_path)
    with pytest.raises(PermissionError, match="no interactive bus"):
        asyncio.run(
            resolver.require_http_get(PermissionDecl(), "any.example.com", None, _ACTOR)
        )


def test_a_pre_6140_blanket_grant_no_longer_short_circuits_the_interactive_prompt(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- the interactive-bus sibling of the test above:
    even WITH a bus available, a pre-#6140 blanket grant no longer
    short-circuits the real per-host prompt -- the operator is asked
    about this host exactly as if no legacy grant existed at all."""
    ApprovalLedger(tmp_path / ".reyn" / "approvals.jsonl").append_approval(
        KEY_WEB_FETCH, True, "workspace",
    )

    bus = _ChoiceBus("always")
    resolver = _resolver(tmp_path)
    asyncio.run(
        resolver.require_http_get(PermissionDecl(), "any.example.com", bus, _ACTOR)
    )
    assert bus.requests, (
        "the pre-#6140 blanket grant short-circuited the prompt -- an "
        "empty list means the removed bare-key read is somehow still live"
    )


def test_a_web_fetch_always_grant_no_longer_covers_an_undeclared_host(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- #6146's own acceptance criterion ⑴, the SECOND
    holder shape: witnesses, via ``bus=None`` (the narrow fallback
    route #6150 already guards -- NOT a real session's own non-
    interactive path, see the sibling test above for that distinction),
    that an ALWAYS answer to the SEPARATE ``web_fetch`` tool prompt
    (``require_web_fetch``, deliberately untouched -- ``web.fetch`` is
    the correct key for ITS OWN axis) no longer implicitly covers an
    undeclared ``http.get`` host -- ``PermissionError`` is reached at
    all, where it used to return silently. A real session's own non-
    interactive route (bus present -> refusal -> ``_approve()`` False)
    is outside what this test covers.

    FALSIFY: restoring the removed bare-key short-circuit makes this go
    red -- no ``PermissionError`` is raised."""
    bus_fetch = _ChoiceBus("always")
    resolver_a = _resolver(tmp_path)
    asyncio.run(resolver_a.require_web_fetch("https://example.com/page", bus_fetch))
    assert bus_fetch.requests, "control arm: the web_fetch prompt must have fired"

    resolver_b = _resolver(tmp_path)
    with pytest.raises(PermissionError, match="no interactive bus"):
        asyncio.run(
            resolver_b.require_http_get(
                PermissionDecl(), "totally-unrelated-host.example", None, _ACTOR,
            )
        )


def test_a_web_fetch_always_grant_does_not_short_circuit_a_declared_wildcard_host(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- #6142's own acceptance criterion (lead-coder):
    an ALWAYS answer to the ``web_fetch`` tool must NOT silently cover a
    DECLARED wildcard ``http.get`` host any more -- that host must still
    reach its own per-host prompt, exactly as if no ``web.fetch`` grant
    existed at all. This is the actual defect #6142 closes (the sibling
    test above documents what #6142 deliberately does NOT close).

    FALSIFY: reverting the fix (checking the bare ``KEY_WEB_FETCH`` key
    before the declared-membership routing, as the code did pre-#6142)
    makes this go red -- ``bus_c.requests == []``, because the bare-key
    short-circuit would find the web_fetch-tool grant before the
    declared per-host prompt ever runs."""
    bus_fetch = _ChoiceBus("always")
    resolver_a = _resolver(tmp_path)
    asyncio.run(resolver_a.require_web_fetch("https://example.com/page", bus_fetch))
    assert bus_fetch.requests

    bus_c = _ChoiceBus("no")
    resolver_c = _resolver(tmp_path)
    wildcard_decl = PermissionDecl(http_get=[{"host": "*"}])
    with pytest.raises(PermissionError, match="denied"):
        asyncio.run(
            resolver_c.require_http_get(
                wildcard_decl, "c.example.com", bus_c, _ACTOR,
            )
        )
    assert bus_c.requests, (
        "the declared-wildcard host C must still be prompted per-host -- an "
        "empty list means the web_fetch-tool ALWAYS grant short-circuited "
        "it (the #6142 defect)."
    )
