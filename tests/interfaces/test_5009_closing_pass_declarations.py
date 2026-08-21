"""Tier 1/2: #5009 closing pass — 3 more snapshot keys declare whether
they are genuinely reported, closing the issue.

Extends `#5009`'s own `cache_usage_reported` field with 3 siblings —
`cron_jobs_reported` / `usage_breakdown_reported` / `ctx_compaction_
reported` — each covering a different snapshot key that was left
undeclared when `#5009` first landed (`#5015`), scoped out on purpose per
architect's own "1 key at a time, report the render site before adding"
process. All 9 originally-open keys were investigated (per-key: where is
it rendered? does the remote-degrade value fabricate anything?), 3 of
the 9 needed declaring, 6 did not — see the class-level docstring for
the disposition of all 9, and `#5009`'s own issue thread for the full
per-key measurement this PR's implementer reported before writing any
of this.

**The corrected criterion** (architect, on this closing pass): the
FIRST measuring stick — "is the remote-degrade value indistinguishable
from a genuine local empty state" — is a useful PROXY, not the actual
rule. `ctx_compaction_reported` is the falsifying case: a real local
session essentially never has a genuine zero-compaction-trigger state
(so the proxy alone would have said "skip it, no confusable real
case"), but the degraded line ("0% to trigger") still FABRICATES a
specific, false reassurance regardless of whether a real state could
produce the same string. `#4996`'s own words already named the real
rule: "never a fabricated turn", "never a fabricated count" — the proxy
is one way a value fabricates, not the definition.

Each of the 3 declared keys, with its own render site and its own
conflation:

- `cron_jobs_reported`: the Cron pane's `["(none)"]` fallback for an
  empty `cron_jobs` list — byte-identical to a genuinely empty LOCAL
  cron config. The clean, original-proxy case (matches `cache_usage_
  reported`'s own shape exactly).
- `usage_breakdown_reported`: the Cost pane's `prompt 0 · completion 0
  · total X` line, X real and nonzero — an inconsistent breakdown
  (`0 + 0 != X`) the pane never flags on its own. Governs the SPLIT
  only; the total stays real and unconditional on both implementations.
- `ctx_compaction_reported`: the Ctx pane's compaction line, degrading
  to `0 / 0 tokens est. (0% to trigger)` — the corrected-criterion case
  above.

Witnesses, same shape as `#5009`'s own original 2: each key strip-
falsified at its own render site (reverting the gate turns the test red
for the stated reason — a fabricated value reappears), paired with an
accept-side test confirming a genuinely-reported figure still renders
unchanged.

Real `LOCAL_CHAT_READ_CAPABILITIES` / `REMOTE_CHAT_READ_CAPABILITIES` /
`project_remote_snapshot` — no mocks.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import (
    cost_pane_lines,
    cron_pane_lines,
    ctx_pane_lines,
)
from reyn.interfaces.repl.read_model import (
    LOCAL_CHAT_READ_CAPABILITIES,
    REMOTE_CHAT_READ_CAPABILITIES,
    cron_jobs_reported_snapshot_key,
    ctx_compaction_reported_snapshot_key,
    project_remote_snapshot,
    usage_breakdown_reported_snapshot_key,
)


def test_capabilities_declare_the_3_closing_pass_keys():
    """Tier 1: the declarations themselves."""
    assert LOCAL_CHAT_READ_CAPABILITIES.cron_jobs_reported is True
    assert LOCAL_CHAT_READ_CAPABILITIES.usage_breakdown_reported is True
    assert LOCAL_CHAT_READ_CAPABILITIES.ctx_compaction_reported is True
    assert REMOTE_CHAT_READ_CAPABILITIES.cron_jobs_reported is False
    assert REMOTE_CHAT_READ_CAPABILITIES.usage_breakdown_reported is False
    assert REMOTE_CHAT_READ_CAPABILITIES.ctx_compaction_reported is False


def test_the_3_helpers_derive_from_the_capabilities_they_are_given():
    """Tier 1: pins each helper's own pure projection, same shape as
    `cache_usage_reported_snapshot_key` — see that function's own
    docstring, in read_model.py, for why a helper rather than a
    hand-typed literal in each producer."""
    assert cron_jobs_reported_snapshot_key(LOCAL_CHAT_READ_CAPABILITIES) == {
        "cron_jobs_reported": True,
    }
    assert usage_breakdown_reported_snapshot_key(REMOTE_CHAT_READ_CAPABILITIES) == {
        "usage_breakdown_reported": False,
    }
    assert ctx_compaction_reported_snapshot_key(LOCAL_CHAT_READ_CAPABILITIES) == {
        "ctx_compaction_reported": True,
    }


def test_remote_snapshot_declares_all_3_unreported():
    """Tier 1: the real producer — `project_remote_snapshot` — carries
    all 3 declarations through, paired with the existing graceful
    degrade values those keys already carried."""
    snap = project_remote_snapshot({})
    assert snap["cron_jobs_reported"] is False
    assert snap["cron_jobs"] == []
    assert snap["usage_breakdown_reported"] is False
    assert snap["ctx_compaction_reported"] is False
    assert snap["ctx_compaction_status_fn"] is None


# ── cron_jobs_reported ──────────────────────────────────────────────────


def test_cron_pane_shows_not_reported_instead_of_a_fabricated_none():
    """Tier 2: strip-falsifier. Reverting the `cron_jobs_reported` gate in
    `cron_pane_lines` turns this red — the pane would show `["(none)"]`,
    indistinguishable from a genuinely empty LOCAL cron config."""
    snap = {"cron_jobs": [], "cron_jobs_reported": False}
    lines = cron_pane_lines(snap)
    assert lines == ["not reported on this connection"], lines


def test_cron_pane_still_lists_real_jobs_when_reported():
    """Tier 2: accept-side — a genuinely reported, non-empty cron config
    renders exactly as before this issue."""
    snap = {
        "cron_jobs": [{"name": "sweep", "schedule": "0 3 * * *", "enabled": True}],
        "cron_jobs_reported": True,
    }
    lines = cron_pane_lines(snap)
    assert lines == ["[on] sweep  0 3 * * *"], lines


# ── usage_breakdown_reported ────────────────────────────────────────────


def test_cost_pane_shows_dashes_instead_of_a_fabricated_zero_split():
    """Tier 2: strip-falsifier. Reverting the `usage_breakdown_reported`
    gate turns this red — the pane would show `prompt 0 · completion 0
    · total 1,540`, an inconsistent breakdown (`0 + 0 != 1,540`) that
    reads as "no tokens used" when real tokens were."""
    snap = {
        "usage": (0, 0, 1540),
        "agent_tokens": 1540,
        "usage_breakdown_reported": False,
    }
    blob = "\n".join(cost_pane_lines(snap))
    assert "prompt — · completion —" in blob, blob
    assert "total 1,540" in blob, (
        f"the total must keep rendering unconditionally (real wire data on "
        f"both implementations):\n{blob}"
    )
    assert "prompt 0" not in blob, blob


def test_cost_pane_still_shows_the_real_split_when_reported():
    """Tier 2: accept-side."""
    snap = {
        "usage": (1200, 340, 1540),
        "agent_tokens": 1540,
        "usage_breakdown_reported": True,
    }
    blob = "\n".join(cost_pane_lines(snap))
    assert "prompt 1,200 · completion 340" in blob, blob
    assert "total 1,540" in blob, blob
    assert "—" not in blob.split("tokens")[1].split("\n")[0], blob


# ── ctx_compaction_reported ─────────────────────────────────────────────


def test_ctx_pane_shows_not_reported_instead_of_a_fabricated_reassurance():
    """Tier 2: strip-falsifier. Reverting the `ctx_compaction_reported`
    gate turns this red — the pane would show `0 / 0 tokens est. (0% to
    trigger)`, a fabricated "plenty of headroom" reading when nothing
    was actually measured."""
    snap = {
        "ctx_window": 200000,
        "ctx_used": 90000,
        "ctx_recent_usage": (90000, 40000),
        "cache_usage_reported": True,
        "ctx_compaction_status_fn": None,
        "ctx_compaction_reported": False,
    }
    blob = "\n".join(ctx_pane_lines(snap))
    assert "compaction   not reported on this connection" in blob, blob
    assert "0% to trigger" not in blob, blob


def test_ctx_pane_still_shows_the_real_compaction_estimate_when_reported():
    """Tier 2: accept-side — a genuinely reported compaction status still
    renders its real figures."""
    def _status_fn():
        return {"effective_trigger": 100000, "free_window": 40000}

    snap = {
        "ctx_window": 200000,
        "ctx_used": 90000,
        "ctx_recent_usage": (90000, 40000),
        "cache_usage_reported": True,
        "ctx_compaction_status_fn": _status_fn,
        "ctx_compaction_reported": True,
    }
    blob = "\n".join(ctx_pane_lines(snap))
    assert "60,000 / 100,000 tokens est." in blob, blob
    assert "60% to trigger" in blob, blob
    assert "not reported" not in blob, blob


# ── pre-attach None/{} — the settled safe direction, all 3 keys ────────


def test_a_pre_attach_snapshot_shows_not_reported_for_all_3_keys_too():
    """Tier 2: the same pre-attach contract #5009's own original 2 keys
    established (`cost_pane_lines(None)`/`ctx_pane_lines(None)` must
    degrade gracefully, never crash) extends to these 3 — and to
    `cron_pane_lines(None)`, not previously exercised by #5009's own
    test file."""
    cost_blob = "\n".join(cost_pane_lines(None))
    assert "prompt — · completion —" in cost_blob, cost_blob

    ctx_blob = "\n".join(ctx_pane_lines(None))
    assert "compaction   not reported on this connection" in ctx_blob, ctx_blob

    cron_lines = cron_pane_lines(None)
    assert cron_lines == ["not reported on this connection"], cron_lines
