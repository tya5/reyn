"""Tier 1/2: #5009 — cache-hit accounting declares WHETHER it is reported,
so ``0`` on a remote client can't be misread as a real 0% hit rate.

Re-opens #4996's own conflation on a DIFFERENT axis. #4996 declared
whether a whole `ChatReadModel` METHOD is supported; this issue is about
two SNAPSHOT KEYS (`session_cached_tokens` / `ctx_recent_usage`) inside
`snapshot()`'s own dict — a `RemoteReadModel` always returns `0`/`(0, 0)`
for both (cache-hit accounting is session-local, never on the AG-UI wire),
indistinguishable on their own from a genuine empty/zero session.

Architect's ruling (co-vet on this issue, in response to a scope question
asked before implementing): fix the KEY, not the PANE — splitting the fix
by which pane reads the key would repeat the exact mistake this issue
names (declaring by implementation/render boundary instead of by the
actual shared fact). `session_cached_tokens` and `ctx_recent_usage` are
ONE fact (cache accounting is or isn't reported on this connection) read
by TWO panes (Cost pane's cumulative line, Ctx pane's last-call line) —
one declaration, `snap["cache_usage_reported"]`, both consumers.

Explicit scope (architect): only these 2 keys. The other 9 session-local
`project_remote_snapshot` keys stay undeclared, filed on #5009 itself for
later, not folded in here. This PR also does NOT touch the owner's
actual "cache stuck at 0%" report — that was measured on a LOCAL session
(owner-confirmed) and turned out to be a separate, since-resolved
display issue (#5011, the single-sample-as-percentage shape), not this
one.

Witness② (lead-coder/architect: "at least 1 location draws differently,
2 is even better; splitting into a pane-scoped fix would leave the OTHER
pane still lying") — both consuming panes are tested, each with the
unreported (marker, not a fabricated 0%) and the reported (accept-side,
unchanged real percentage) case.

Real `RegistryReadModel`/`RemoteReadModel`-shaped snapshot construction
(`project_remote_snapshot` directly, and a real local snapshot via
`test_3338`'s own `_real_snapshot` helper) — no mocks.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import cost_pane_lines, ctx_pane_lines
from reyn.interfaces.repl.read_model import (
    LOCAL_CHAT_READ_CAPABILITIES,
    REMOTE_CHAT_READ_CAPABILITIES,
    project_remote_snapshot,
)


def test_remote_snapshot_declares_cache_usage_unreported():
    """Tier 1: the declaration itself — a remote snapshot always says
    `cache_usage_reported: False`, paired with the graceful `0`/`(0, 0)`
    values those same 2 keys already carried."""
    snap = project_remote_snapshot({})
    assert snap["cache_usage_reported"] is False
    assert snap["session_cached_tokens"] == 0
    assert snap["ctx_recent_usage"] == (0, 0)


def test_capabilities_declarations_are_unaffected_by_this_key():
    """Tier 1: pins that #5009 is a SNAPSHOT-key declaration, layered
    alongside #4996's METHOD-level `ChatReadModelCapabilities` — not a
    replacement for it. Both declarations exist independently on their
    respective read models."""
    assert LOCAL_CHAT_READ_CAPABILITIES.conversation_history is True
    assert REMOTE_CHAT_READ_CAPABILITIES.conversation_history is False


def test_cost_pane_shows_not_reported_instead_of_a_fabricated_zero_percent():
    """Tier 2: witness② path ① (Cost pane). Strip-falsifier: reverting
    `_cache_hit_line`'s `reported` gate turns this red — the pane would
    show `0% hit (0 / 0 prompt tokens, cumulative)`, indistinguishable
    from a genuine empty session."""
    snap = {
        "usage": (0, 0, 0),
        "agent_tokens": 0,
        "session_cached_tokens": 0,
        "cache_usage_reported": False,
    }
    blob = "\n".join(cost_pane_lines(snap))
    assert "not reported on this connection" in blob, blob
    assert "0% hit" not in blob, (
        f"a fabricated 0% must not appear when reporting is declared "
        f"unavailable:\n{blob}"
    )


def test_cost_pane_still_shows_a_real_percentage_when_reported():
    """Tier 2: accept-side for the Cost pane — a genuinely reported,
    non-zero cache figure renders exactly as before this issue. Without
    this, an "always show not-reported" implementation would pass the
    test above vacuously."""
    snap = {
        "usage": (12345, 6789, 19134),
        "agent_tokens": 19134,
        "session_cached_tokens": 5180,
        "cache_usage_reported": True,
    }
    blob = "\n".join(cost_pane_lines(snap))
    assert "42% hit" in blob, blob
    assert "not reported" not in blob, blob


def test_ctx_pane_shows_not_reported_instead_of_a_fabricated_zero_percent():
    """Tier 2: witness② path ② (Ctx pane) — the SAME declaration consulted
    by a SECOND, independent consumer. Strip-falsified the same way as
    the Cost pane test above."""
    snap = {
        "ctx_window": 200000,
        "ctx_used": 48120,
        "ctx_recent_usage": (0, 0),
        "cache_usage_reported": False,
    }
    blob = "\n".join(ctx_pane_lines(snap))
    assert "not reported on this connection" in blob, blob
    assert "0% hit" not in blob, blob


def test_ctx_pane_still_shows_a_real_percentage_when_reported():
    """Tier 2: accept-side for the Ctx pane."""
    snap = {
        "ctx_window": 200000,
        "ctx_used": 48120,
        "ctx_recent_usage": (48120, 14900),
        "cache_usage_reported": True,
    }
    blob = "\n".join(ctx_pane_lines(snap))
    assert "31% hit" in blob, blob
    assert "not reported" not in blob, blob


def test_a_read_model_that_omits_the_key_defaults_to_reported():
    """Tier 2: backward compatibility — a snapshot dict that predates this
    key (any 3rd-party or test-double `ChatReadModel` that hasn't been
    updated) defaults to `reported=True`, preserving the pre-#5009
    rendering rather than newly hiding a real figure."""
    snap = {
        "usage": (100, 50, 150),
        "agent_tokens": 150,
        "session_cached_tokens": 30,
    }
    blob = "\n".join(cost_pane_lines(snap))
    assert "not reported" not in blob, blob
    assert "% hit" in blob, blob
