"""Tier 2: #5011 — the Ctx pane's cache line names WHAT it measured.

Owner-observed tonight: the Ctx pane's cache line (`chrome.py`,
`ctx_pane_lines`) reads `ctx_recent_usage` — `Session.last_call_usage`,
"the SINGLE MOST RECENT LLM call's prompt_tokens" (`status.py`'s own
docstring, verbatim) — and renders it as `N% hit (a / b prompt tokens)`
with NO note saying so, unlike the Cost pane's cumulative cache line
(`note="cumulative"`, present since #3338). A percentage-shaped number
with no qualifier reads as a RATE; the owner (and architect, independently,
30 minutes of real investigation before landing on "it's the display, not
the cache") read it that way. If the single most recent call happened to
be cache-miss (a small auxiliary call, for example), the line reads
`0% hit` — indistinguishable from "caching stopped working" without
reading `status.py`'s source.

Fix (architect's option A, chosen as the minimal, symmetric one —
architect: "cumulative side already has a note, matching it is the
natural pairing"): the Ctx pane's own `_cache_hit_line` call now passes
`note="last call"`, the exact asymmetry architect's own issue named as
the root cause.

**UX-visible change, flagged explicitly:** the Ctx pane's cache line now
reads `cache      N% hit (a / b prompt tokens, last call)` instead of
`cache      N% hit (a / b prompt tokens)` — an added qualifier, wording
is the author's and freely revisable by the owner.

Explicitly NOT this issue's claim (per its own "what this issue does not
say" section): the cache mechanism itself was never broken — the owner
confirmed via the Cost pane's cumulative figure rising over time. This
is a display-only fix. Also independent of `#5009` (that one's about
remote's `0`/`(0, 0)` degrade being indistinguishable from a real zero;
this one reproduces on LOCAL too, and is about a single-sample figure
looking like a rate).

Witness, per lead-coder's own acceptance note on the issue: assert what
the note SAYS, not merely that a note is present — "出る" alone would
also pass if the wrong word landed there.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import cost_pane_lines, ctx_pane_lines


def test_ctx_pane_cache_line_names_itself_last_call():
    """Tier 2: the Ctx pane's cache line reads `last call`, not silence —
    the exact asymmetry with the Cost pane's `cumulative` note this issue
    exists to close."""
    snap = {
        "ctx_window": 200000,
        "ctx_used": 48120,
        "ctx_recent_usage": (48120, 14900),
        "cache_usage_reported": True,
    }
    blob = "\n".join(ctx_pane_lines(snap))
    assert "31% hit" in blob, blob
    assert "last call" in blob, (
        f"the Ctx pane's cache line must name what it measured, the same "
        f"way the Cost pane's cumulative line already does:\n{blob}"
    )


def test_cost_pane_cache_line_still_names_itself_cumulative():
    """Tier 2: accept-side / non-regression — the OTHER cache line
    (Cost pane, `session_cached_tokens`) already had its own note
    (`cumulative`, since #3338) and must keep it unchanged. Without this,
    a fix that accidentally overwrote BOTH lines' notes with the same
    word would still pass the test above."""
    snap = {
        "usage": (12345, 6789, 19134),
        "agent_tokens": 19134,
        "session_cached_tokens": 5180,
        # #5771 stage②: the Cost pane's OWN split-off axis, not the Ctx
        # pane's cache_usage_reported (see chrome.py's own comment at
        # this pane's _cache_hit_line call site for the split).
        "session_cache_usage_reported": True,
    }
    blob = "\n".join(cost_pane_lines(snap))
    assert "42% hit" in blob, blob
    assert "cumulative" in blob, blob
    assert "last call" not in blob, (
        f"the Cost pane's note must stay 'cumulative', not the Ctx pane's "
        f"'last call':\n{blob}"
    )


def test_a_cache_miss_on_the_single_recent_call_still_says_last_call():
    """Tier 2: the exact failure mode the owner hit — the single most
    recent call happens to be a cache miss (a small auxiliary call, e.g.)
    and the line reads `0% hit`. With the note present, that `0%` is
    legible as "this one call missed", not "caching stopped working" —
    the note is what carries that distinction, so it must survive even
    at the 0% edge, not just the non-zero happy path the tests above
    cover."""
    snap = {
        "ctx_window": 200000,
        "ctx_used": 500,
        "ctx_recent_usage": (500, 0),
        "cache_usage_reported": True,
    }
    blob = "\n".join(ctx_pane_lines(snap))
    assert "0% hit" in blob, blob
    assert "last call" in blob, (
        f"the note must not silently drop at the 0%-hit edge:\n{blob}"
    )
