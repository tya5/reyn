"""Tier 1/2: #5009 — cache-hit accounting declares WHETHER it is reported,
so ``0`` on a remote client can't be misread as a real 0% hit rate.

Re-opens #4996's own conflation on a DIFFERENT axis. #4996 declared
whether a whole `ChatReadModel` METHOD is supported; this issue is about
two `snapshot()` dict KEYS (`session_cached_tokens` / `ctx_recent_usage`)
— a `RemoteReadModel` always returns `0`/`(0, 0)` for both (cache-hit
accounting is session-local, never on the AG-UI wire), indistinguishable
on their own from a genuine empty/zero session.

**Design history, kept honest** (this issue went through 2 falsified
attempts before settling — both measured, not guessed):

1. Fix the KEY, not the pane (architect, in response to a scope question
   asked before implementing): `session_cached_tokens`/`ctx_recent_usage`
   are ONE fact read by TWO panes (Cost pane's cumulative line, Ctx
   pane's last-call line) — splitting the fix by pane would repeat this
   issue's own named mistake. Settled, still true.
2. First container tried: a hand-typed `"cache_usage_reported": True/False`
   literal directly in each `snapshot()` producer, with panes reading
   `snap.get(key, True)`. FALSIFIED by running the actual test suite: a
   `True` default lets a producer that forgot the key silently CLAIM
   "I report" while returning a fabricated `0`% — the exact conflation
   this issue exists to close, just moved one level down.
3. Second attempt: flip the default to `snap.get(key, False)`. Also
   FALSIFIED, on measurement: "snapshot not built yet" (`None`/`{}`,
   the genuine pre-attach case `cost_pane_lines(None)`/`ctx_pane_lines
   (None)` already have a Tier 1 contract for — see
   `test_no_placeholder_residue_pickers_empty_readouts_zero`) and
   "snapshot built but this ONE key forgotten" both normalize to the
   same `{}` shape at `snap = snap or {}` — no default value, in either
   direction, can tell them apart. The conflation this issue exists to
   close cannot be fixed by choosing a default; it needs a DIFFERENT
   container for the declaration.
4. Settled: the declaration moves INTO `ChatReadModelCapabilities`
   itself (#4996's own container, already disciplined for exactly this:
   frozen, all fields required, no defaults — a producer that forgets
   the field fails to CONSTRUCT, at import time, not at render time).
   Each `snapshot()` producer derives its own key from ITS OWN
   capabilities constant via `reported_snapshot_keys(...)` (originally
   `cache_usage_reported_snapshot_key`, generalized in #5009's closing
   pass once 3 more fields needed the identical helper shape — see that
   function's own docstring) — never hand-typed a second time, so the
   two producers cannot silently diverge from each other. The pane's
   own `snap.get(key, False)` default is now correct and UNREACHABLE
   for any real, complete read model: it fires only for the genuine
   `None`/`{}` pre-attach case, where `False` (never claim reporting
   with nothing to consult) is the right answer.

Explicit scope (architect): only these 2 keys folded into
`ChatReadModelCapabilities`. The other 9 session-local
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
unchanged real percentage) case. Witness① (a producer that forgets the
field fails to construct) is covered generically by
``test_4996_read_model_capabilities.py``'s own witness① tests, extended
to this field rather than duplicated here — one dataclass, one guard.

Real `project_remote_snapshot` / `LOCAL_CHAT_READ_CAPABILITIES` /
`REMOTE_CHAT_READ_CAPABILITIES` — no mocks.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import cost_pane_lines, ctx_pane_lines
from reyn.interfaces.repl.read_model import (
    LOCAL_CHAT_READ_CAPABILITIES,
    REMOTE_CHAT_READ_CAPABILITIES,
    project_remote_snapshot,
    reported_snapshot_keys,
)


def test_capabilities_declare_cache_usage_reported():
    """Tier 1: the declaration itself, on both constants — the SSoT every
    `snapshot()` producer must derive from rather than hand-type."""
    assert LOCAL_CHAT_READ_CAPABILITIES.cache_usage_reported is True
    assert REMOTE_CHAT_READ_CAPABILITIES.cache_usage_reported is False


def test_the_snapshot_key_helper_derives_from_the_capabilities_it_is_given():
    """Tier 1: `reported_snapshot_keys` is a pure projection over EVERY
    field of the capabilities it's given (generalized in #5009's closing
    pass from the original single-field `cache_usage_reported_snapshot_
    key` — see that function's own docstring) — its `cache_usage_
    reported` entry always matches the source it was given, nothing
    hand-typed alongside it. Both real producers call this (see the
    tests below), so this pins the ONE function they both depend on."""
    assert reported_snapshot_keys(LOCAL_CHAT_READ_CAPABILITIES)["cache_usage_reported"] is True
    assert reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES)["cache_usage_reported"] is False


def test_remote_snapshot_declares_cache_usage_unreported():
    """Tier 1: the REAL producer — `project_remote_snapshot` — carries the
    declaration through to its own output, paired with the graceful
    `0`/`(0, 0)` values those same 2 keys already carried."""
    snap = project_remote_snapshot({})
    assert snap["cache_usage_reported"] is False
    assert snap["session_cached_tokens"] == 0
    assert snap["ctx_recent_usage"] == (0, 0)


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
    """Tier 2: accept-side for the Ctx pane — a genuinely reported cache
    figure renders its real percentage and does NOT degrade to this row's
    own "not reported".

    #5588: the assertion is scoped to the CACHE line. It used to be a
    blanket ``"not reported" not in blob`` over the whole pane, which made
    every OTHER independently-reported row this pane grows a silent
    tripwire for a test that is not about them — this test's own previous
    docstring already had to warn that the compaction row "must not fall
    back to its own 'not reported' and get caught by the blanket
    assertion below", and the ``folded`` row (#5578's persisted watermark)
    was the second to hit it. Scoping keeps the claim identical and stops
    it from failing for a reason it never meant to test."""
    snap = {
        "ctx_window": 200000,
        "ctx_used": 48120,
        "ctx_recent_usage": (48120, 14900),
        "cache_usage_reported": True,
        "ctx_compaction_reported": True,
    }
    blob = "\n".join(ctx_pane_lines(snap))
    assert "31% hit" in blob, blob
    (cache_line,) = [ln for ln in ctx_pane_lines(snap) if ln.startswith("cache")]
    assert "not reported" not in cache_line, cache_line


def test_a_pre_attach_snapshot_defaults_to_not_reported_not_a_crash():
    """Tier 2: the genuinely empty `None`/`{}` pre-attach case (an
    existing Tier 1 contract — `test_no_placeholder_residue_pickers_
    empty_readouts_zero` calls `cost_pane_lines(None)`/`ctx_pane_lines
    (None)` directly and requires graceful zero output, never a crash).
    `snap.get("cache_usage_reported", False)` is what makes this
    reachable at all WITHOUT lying: `False` here never claims a real
    percentage — it is the safe direction, distinct from (and no longer
    confused with) a real read model that forgot to declare, which now
    fails to CONSTRUCT instead (`ChatReadModelCapabilities`'s own
    witness①, `test_4996_read_model_capabilities.py`)."""
    assert cost_pane_lines(None) is not None  # does not raise
    blob = "\n".join(cost_pane_lines(None))
    assert "not reported on this connection" in blob, blob
    assert "% hit" not in blob, blob

    ctx_blob = "\n".join(ctx_pane_lines(None))
    assert "not reported on this connection" in ctx_blob, ctx_blob
