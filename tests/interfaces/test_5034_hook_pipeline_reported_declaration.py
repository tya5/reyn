"""Tier 1/2: #5034 — hook and pipeline panes declare whether they are
genuinely reported, closing the same conflation #5009/#5027 already
closed for cache/cron/usage/compaction, on 2 keys #5009's own
hand-enumerated list missed.

**How the 2 keys were found** (not hand-enumerated a second time,
architect's explicit condition on #5034): `project_remote_snapshot`'s
own return dict was AST-walked to collect every literal (non-``v.get``)
entry — 19 keys total, posted on #5034's own thread as the population.
10 of the 19 were already dispositioned (4 declared, 6 dropped with
reason on #5009's own thread). Of the 9 keys #5009's hand count never
saw: `hooks`/`hook_items` and `pipelines` genuinely fabricate (this
issue); `skills`/`visibility_items`/`mcp_subscriptions`/
`unknown_config_key_count`/`unknown_config_keys`/`ctx_source` do not
(traced mechanically to their own render sites on #5034's thread —
`skills` in particular decided by `visibility_items`'s own None/`[]`
split, the same shape already ruled for `mcp_servers`).

Each declared key, its own render site and its own conflation:

- `hooks_reported`: `_hook_pane_entries`'s `["(none)"]` fallback for an
  empty `hook_items`/`hooks` pair — byte-identical to a genuinely empty
  LOCAL hook config.
- `pipelines_reported`: `pipe_pane_lines`'s `["(none)"]` fallback for an
  empty `pipelines` list — same shape.

Witnesses, same shape as #5009/#5027's own: each key strip-falsified at
its own render site, paired with an accept-side test confirming a
genuinely-reported figure still renders unchanged.

Real `LOCAL_CHAT_READ_CAPABILITIES` / `REMOTE_CHAT_READ_CAPABILITIES` /
`project_remote_snapshot` — no mocks.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import (
    _hook_pane_entries,
    pipe_pane_lines,
)
from reyn.interfaces.repl.read_model import (
    LOCAL_CHAT_READ_CAPABILITIES,
    REMOTE_CHAT_READ_CAPABILITIES,
    project_remote_snapshot,
    reported_snapshot_keys,
)


def test_capabilities_declare_the_2_keys():
    """Tier 1: the declarations themselves."""
    assert LOCAL_CHAT_READ_CAPABILITIES.hooks_reported is True
    assert LOCAL_CHAT_READ_CAPABILITIES.pipelines_reported is True
    assert REMOTE_CHAT_READ_CAPABILITIES.hooks_reported is False
    assert REMOTE_CHAT_READ_CAPABILITIES.pipelines_reported is False


def test_the_shared_helper_derives_both_from_the_capabilities_given():
    """Tier 1: pins `reported_snapshot_keys`'s own pure projection for
    these 2 fields — no new wiring, the existing generic helper already
    returns every field."""
    local_keys = reported_snapshot_keys(LOCAL_CHAT_READ_CAPABILITIES)
    assert local_keys["hooks_reported"] is True
    assert local_keys["pipelines_reported"] is True

    remote_keys = reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES)
    assert remote_keys["hooks_reported"] is False
    assert remote_keys["pipelines_reported"] is False


def test_remote_snapshot_declares_both_unreported():
    """Tier 1: the real producer — `project_remote_snapshot` — carries
    both declarations through, paired with the existing graceful degrade
    values those keys already carried."""
    snap = project_remote_snapshot({})
    assert snap["hooks_reported"] is False
    assert snap["hooks"] == []
    assert snap["hook_items"] == []
    assert snap["pipelines_reported"] is False
    assert snap["pipelines"] == []


# ── hooks_reported ──────────────────────────────────────────────────────


def test_hook_pane_shows_not_reported_instead_of_a_fabricated_none():
    """Tier 2: strip-falsifier. Reverting the `hooks_reported` gate in
    `_hook_pane_entries` turns this red — the pane would show
    `["(none)"]`, indistinguishable from a genuinely empty LOCAL hook
    config."""
    snap = {"hook_items": [], "hooks": [], "hooks_reported": False}
    entries = _hook_pane_entries(snap)
    rows = [row for row, _cmd in entries]
    assert rows == ["not reported on this connection"], rows


def test_hook_pane_still_lists_real_hooks_when_reported():
    """Tier 2: accept-side — a genuinely reported, non-empty hook config
    renders exactly as before this issue."""
    snap = {
        "hook_items": [{"name": "pre-commit", "on": True, "scope": "session"}],
        "hooks_reported": True,
    }
    entries = _hook_pane_entries(snap)
    rows = [row for row, _cmd in entries]
    assert rows == ["[on] pre-commit  · session"], rows


def test_hook_pane_config_only_fallback_still_works_when_reported():
    """Tier 2: accept-side, the read-only `hooks` fallback (no session-
    backed `hook_items`) still renders when genuinely reported."""
    snap = {
        "hook_items": [],
        "hooks": [{"label": "lint-on-save"}],
        "hooks_reported": True,
    }
    entries = _hook_pane_entries(snap)
    rows = [row for row, _cmd in entries]
    assert rows == ["lint-on-save"], rows


# ── pipelines_reported ──────────────────────────────────────────────────


def test_pipe_pane_shows_not_reported_instead_of_a_fabricated_none():
    """Tier 2: strip-falsifier. Reverting the `pipelines_reported` gate
    in `pipe_pane_lines` turns this red — the pane would show
    `["(none)"]`, indistinguishable from a genuinely empty LOCAL pipeline
    registry."""
    snap = {"pipelines": [], "pipelines_reported": False}
    lines = pipe_pane_lines(snap)
    assert lines == ["not reported on this connection"], lines


def test_pipe_pane_still_lists_real_pipelines_when_reported():
    """Tier 2: accept-side — a genuinely reported, non-empty pipeline
    registry renders exactly as before this issue."""
    snap = {
        "pipelines": [{"name": "release", "description": "cut a release"}],
        "pipelines_reported": True,
    }
    lines = pipe_pane_lines(snap)
    assert lines == ["release  cut a release"], lines


# ── pre-attach None/{} — the settled safe direction, both keys ─────────


def test_a_pre_attach_snapshot_shows_not_reported_for_both_keys_too():
    """Tier 2: the same pre-attach contract #5009's own keys established
    extends to these 2 — `pipe_pane_lines(None)` was already exercised
    for the graceful-empty contract; this pins the "not reported" wording
    now takes over that same call."""
    pipe_lines = pipe_pane_lines(None)
    assert pipe_lines == ["not reported on this connection"], pipe_lines

    hook_entries = _hook_pane_entries({})
    hook_rows = [row for row, _cmd in hook_entries]
    assert hook_rows == ["not reported on this connection"], hook_rows
