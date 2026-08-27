"""Tier 2: #5236 — the moment an operator GRANTS a permanent approval (the
ALWAYS choice on an interactive permission prompt) emits a P6 audit-event.
Before this, only the undo half of the pair (``permission_approval_revoked``/
``_cleared``, #5065) was observable — the audit trail could record that a
decision was taken back, but never that it was made.

Real ``PermissionResolver`` + a real interactive-choice ``InterventionBus``
throughout (mirrors ``tests/security/test_permission_collapse_phase3.py``'s
own ``_AlwaysBus`` harness for driving the ALWAYS choice) — no mock. Witness
reads the actual ``.reyn/events/direct/permission_prompt/`` files this PR's
``emit_direct_event`` call writes (mirrors
``tests/web/test_5065_permissions_router_emits_audit_event.py``'s own
``_read_direct_web_events`` shape, generalized to a different ``surface``).

Strip-falsifier: comment out the ``emit_direct_event(...)`` call in
``permissions.py``'s ``_persist`` — the approval still lands (``fold()``
still shows it granted), but no new ``.reyn/events`` file/line appears,
turning the witness red. Verified for real in this PR's own commit message.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.security.permissions.approval_ledger import ApprovalLedger
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention


class _ChoiceBus(InterventionBus):
    """A real (non-mock) ``InterventionBus`` that always answers with one
    fixed choice — the same shape ``test_permission_collapse_phase3.py``'s
    own ``_AlwaysBus`` uses to drive the ALWAYS branch."""

    def __init__(self, choice_id: str) -> None:
        self.requests: "list[UserIntervention]" = []
        self._choice_id = choice_id

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id=self._choice_id)


def _read_direct_permission_prompt_events(project_root: Path) -> "list[dict]":
    """Read every event line under
    ``.reyn/events/direct/permission_prompt/`` — this PR's new
    ``surface="permission_prompt"`` directory."""
    surface_dir = project_root / ".reyn" / "events" / "direct" / "permission_prompt"
    if not surface_dir.is_dir():
        return []
    out: "list[dict]" = []
    for f in sorted(surface_dir.glob("*/*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_the_always_choice_emits_one_audit_event_naming_the_granted_key(
    tmp_path: Path,
) -> None:
    """Tier 2: driving a real permission prompt to its ALWAYS choice ->
    exactly one new ``.reyn/events`` entry, naming the GRANTED key (not
    just "one more event appeared" — matching the strengthened,
    key-specific witness lead-coder required for the revoke side, #5065)."""
    assert _read_direct_permission_prompt_events(tmp_path) == []

    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "example.com"}])
    bus = _ChoiceBus("always")
    asyncio.run(resolver.require_http_get(decl, "example.com", bus, "test_skill"))
    assert bus.requests, "control arm: the prompt must have fired at all"

    events = _read_direct_permission_prompt_events(tmp_path)
    assert events, "expected an audit event for the grant, got none"
    assert events[1:] == [], f"expected exactly one audit event, got {events}"
    assert events[0]["type"] == "permission_approval_granted"
    assert events[0]["data"]["key"] == "test_skill/http.get/example.com"
    assert events[0]["data"]["surface"] == "permission_prompt"

    # The write itself lands, unaffected by the audit addition.
    saved, _bound = ApprovalLedger(tmp_path / ".reyn" / "approvals.jsonl").fold()
    assert saved.get("test_skill/http.get/example.com") is True


def test_a_repeat_call_after_always_is_silent_and_emits_nothing_new(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity for "exactly once" — a SECOND call for the same,
    already-persisted key must not re-prompt (test_permission_collapse_
    phase3.py's own "must not re-prompt" invariant) and therefore must not
    emit a second grant event either; ``_persist`` is only reached via a
    live prompt answer, never via the persisted-lookup fast path."""
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "example.com"}])
    bus = _ChoiceBus("always")
    asyncio.run(resolver.require_http_get(decl, "example.com", bus, "test_skill"))
    first_events = _read_direct_permission_prompt_events(tmp_path)
    assert [e["type"] for e in first_events] == ["permission_approval_granted"]
    requests_after_first_call = list(bus.requests)

    asyncio.run(resolver.require_http_get(decl, "example.com", bus, "test_skill"))

    assert bus.requests == requests_after_first_call, "the second call must not re-prompt"
    assert _read_direct_permission_prompt_events(tmp_path) == first_events, (
        "a call that never re-persists must not emit a second grant event"
    )


def test_the_never_choice_denial_emits_nothing(tmp_path: Path) -> None:
    """Tier 2: falsification pair — the NEVER choice (deny + persist,
    ``_persist(key, False)`` — this repo's real "unknown/no" fallback is
    the same session-only-deny branch YES already covers structurally, so
    NEVER is the meaningful non-grant persist to check) must emit no
    ``permission_approval_granted``. Without this, a naive "emit whenever
    _persist is CALLED, ignoring approved's value" implementation would
    still pass the positive test above (it calls _persist with True there)
    while ALSO wrongly emitting for every revoke — exactly the double-count
    ``test_5065_permissions_router_emits_audit_event.py``'s own suite would
    then start silently accumulating alongside this file's local gates."""
    assert _read_direct_permission_prompt_events(tmp_path) == []

    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(http_get=[{"host": "example.com"}])
    bus = _ChoiceBus("never")
    with pytest.raises(PermissionError):
        asyncio.run(resolver.require_http_get(decl, "example.com", bus, "test_skill"))

    assert _read_direct_permission_prompt_events(tmp_path) == []
    # The persisted-False row (a revoke-shaped record for a key that was
    # never granted) is real too — confirms _persist(key, False) genuinely
    # ran, so the absence of an event above is a real non-emission, not a
    # vacuous "nothing happened at all".
    saved, _bound = ApprovalLedger(tmp_path / ".reyn" / "approvals.jsonl").fold()
    assert saved.get("test_skill/http.get/example.com") is False
