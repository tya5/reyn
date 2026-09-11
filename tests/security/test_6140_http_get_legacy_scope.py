"""Tier 2: #6140 — the no-declaration ``http.get`` legacy-compat path in
``require_http_get`` recorded an ALWAYS answer under the bare ``KEY_WEB_
FETCH`` constant (no host in the key at all), so ONE "Allow fetching from
{host!r}?" answer silently authorised EVERY future host — including hosts
a DIFFERENT actor's own DECLARED wildcard would otherwise have prompted
for individually (the ``:1947`` bare-key short-circuit fired before that
per-host prompt ever ran).

Real ``PermissionResolver`` + a real interactive-choice ``InterventionBus``
throughout (the ``_ChoiceBus`` shape ``test_5052_approval_scope_dimension.
py`` / ``test_5236_permission_approval_granted_audit_event.py`` already
establish) — no mocks. Each of the 3 issue-body acceptance criteria gets
its own test, plus two lead-coder BLOCKING rounds on PR #6141:

- Round 1 (#6140's own ② — "the window names no end"): the bare-key READ
  that survives for a PRE-#6140 persisted grant must be VISIBLE and name
  its own concrete removal condition (#6142), never a dateless
  "deprecation window" repeated silently.
- Round 2, finding B (measured): a bare ``warnings.warn(...,
  DeprecationWarning)`` never reaches a real operator (Python's own
  default filter ignores ``DeprecationWarning`` outside ``__main__``) —
  the fix uses ``logger.warning`` instead, asserted here via ``caplog``.
- Round 2, finding A (measured, ``permissions.py:2634`` on
  ``origin/main``): ``require_web_fetch`` — a separate method/tool,
  deliberately untouched by this PR — still writes a NEW grant under
  the same bare ``KEY_WEB_FETCH`` key, which ``require_http_get``'s own
  surviving read then honors for ANY host. Documented, not fixed, here.
"""
from __future__ import annotations

import asyncio
import logging
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


# ── the surviving bare-key READ (pre-#6140 grants): visible, not silent ──


def test_a_pre_6140_blanket_grant_still_authorises_but_now_logs_with_an_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: accept -- lead-coder BLOCKING (PR #6141): the ``:1947``-area
    bare-``KEY_WEB_FETCH`` READ is intentionally kept (dropping it would
    turn a working non-interactive run into a hard ``PermissionError``
    for anyone still holding a PRE-#6140 grant) -- but #6140's own ②
    ("the window names no end") means keeping it silent is not
    acceptable either. A real pre-#6140-shaped ledger entry (the bare
    key, no host) is written directly here -- never through the FIXED
    code path, which can no longer produce this shape at all -- to
    reproduce exactly what an operator's already-existing approvals.yaml
    looks like.

    Uses ``logger.warning`` (``caplog``), never ``warnings.warn`` — a
    2nd lead-coder BLOCKING on this same PR, measured: Python's own
    default filter is ``('ignore', None, DeprecationWarning, None, 0)``
    for any module that is not ``__main__`` (``permissions.py`` never
    is), so a bare ``warnings.warn(DeprecationWarning, ...)`` never
    reaches an operator in production — only pytest's own
    ``filterwarnings`` config made an earlier version of this test look
    green. ``caplog`` reads the real logging pipeline `reyn.log` itself
    writes through, not a pytest-only filter override.

    FALSIFY: removing the ``logger.warning(...)`` call at the ``:1947``
    read site makes this go red (``caplog.records`` empty) while the
    grant still silently authorises the host -- the exact "invisible,
    not just backward-compatible" shape this test exists to catch.
    """
    ApprovalLedger(tmp_path / ".reyn" / "approvals.jsonl").append_approval(
        KEY_WEB_FETCH, True, "workspace",
    )

    bus = _ChoiceBus("no")  # must NEVER be consulted -- already short-circuited
    resolver = _resolver(tmp_path)
    with caplog.at_level(logging.WARNING, logger="reyn.security.permissions.permissions"):
        asyncio.run(
            resolver.require_http_get(PermissionDecl(), "any.example.com", bus, _ACTOR)
        )
    assert bus.requests == [], "control arm: the legacy blanket grant must still short-circuit"

    matching = [r for r in caplog.records if "any.example.com" in r.getMessage()]
    assert matching, f"expected a warning naming the legacy grant, got {caplog.records!r}"
    message = matching[0].getMessage()
    assert "6142" in message, f"expected the removal-condition issue number in the log: {message!r}"
    assert "approvals.yaml" in message, f"expected how-to-end instructions in the log: {message!r}"


# ── finding A (lead-coder BLOCKING #2, measured): require_web_fetch can
#      still CREATE a new bare-key grant require_http_get then reads ──


def test_a_web_fetch_tool_approval_is_read_by_require_http_get_for_any_host(
    tmp_path: Path,
) -> None:
    """Tier 2: accept -- documents (does NOT claim to close) a real,
    CURRENTLY-reachable conflation lead-coder's own review measured:
    ``require_web_fetch`` (a separate tool/method, deliberately
    untouched by this fix) still writes under the bare ``KEY_WEB_FETCH``
    key when an operator answers ALWAYS to a `web_fetch` tool prompt.
    ``require_http_get``'s own ``:1947`` read cannot distinguish that
    grant from an http.get-axis one, so a web_fetch-tool consent is
    read as blanket http.get authorization for ANY host -- not merely
    pre-#6140 residue. Narrowing this read's own scope is tracked at
    #6142, same as the pre-#6140-residue removal -- NOT fixed by this
    PR (require_web_fetch is out of #6140's own scope)."""
    bus_fetch = _ChoiceBus("always")
    resolver_a = _resolver(tmp_path)
    asyncio.run(resolver_a.require_web_fetch("https://example.com/page", bus_fetch))
    assert bus_fetch.requests, "control arm: the web_fetch prompt must have fired"

    bus_http_get = _ChoiceBus("no")  # must NEVER be consulted -- short-circuited
    resolver_b = _resolver(tmp_path)
    asyncio.run(
        resolver_b.require_http_get(
            PermissionDecl(), "totally-unrelated-host.example", bus_http_get, _ACTOR,
        )
    )
    assert bus_http_get.requests == [], (
        "the web_fetch-tool grant was read as http.get authorization for an "
        "unrelated host -- the conflation this test documents, tracked at #6142"
    )
