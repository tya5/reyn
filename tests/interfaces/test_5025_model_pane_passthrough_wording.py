"""Tier 2: #5025 — the model picker's "current, not in the class list"
row asserts a weaker, always-true claim instead of a false one.

The old wording, ``"(current, not a configured class)"``, was FALSE on a
remote connection: the snapshot's ``model_classes`` is unconditionally
``[]`` there (never reported over the wire — the same #5009 conflation,
here on a VALUE `_model_pane_entries` is handed rather than a declared
capability key), so the row asserted "nothing is configured" regardless
of what the operator actually configured server-side.

Settled (architect + lead-coder, issuecomment-5374381732): wording only,
no new `ChatReadModelCapabilities` field — `classes`/`active` never
fabricate a VALUE here, only the accompanying claim overreached. New
wording: ``"(current model, not in the class list below)"`` — literally
true in BOTH cases this function actually distinguishes:

- genuine local #3324 passthrough — nothing below really IS configured.
- remote — the list below is empty because it is never populated,
  configured or not.

PROVISIONAL (same footing as #5011's ``note="last call"``): the exact
string is the author's, freely revisable by the owner without touching
`_model_pane_entries`'s logic. Scope, per lead-coder's own sign-off: THIS
row only — no declaration added, no other pane swept.

Witness ①② (lead-coder, stated before implementation): the new string
itself must be asserted (not just "a row exists") in BOTH the local-
passthrough case and the remote-shaped case (`classes == []`) — without
both, a "remote-only" branch would still pass the local case vacuously,
and vice versa. Neither case is told apart by the function — that is the
point of the weaker claim, not a gap in this test.

Real `_model_pane_entries` / `model_pane_options` — no mocks.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import (
    _model_pane_entries,
    model_pane_options,
)

_NEW_WORDING = "(current model, not in the class list below)"
_OLD_WORDING = "(current, not a configured class)"


def test_local_passthrough_shows_the_weaker_wording():
    """Tier 2: #3324's own local case — a raw model string active, real
    configured classes present, none matching it."""
    entries = _model_pane_entries(["fast", "careful"], "openrouter/some-raw-id")
    rows = [row for row, _cmd in entries]
    assert f"{_NEW_WORDING}  openrouter/some-raw-id" in rows, rows
    assert not any(_OLD_WORDING in row for row in rows), rows


def test_remote_shaped_classes_show_the_same_weaker_wording():
    """Tier 2: the remote shape — `classes == []` (never reported over the
    wire), active still a real model name. The row must not claim "not
    configured" when nothing here can tell configured-but-unreported
    apart from genuinely unconfigured."""
    entries = _model_pane_entries([], "claude-sonnet-5")
    rows = [row for row, _cmd in entries]
    assert f"{_NEW_WORDING}  claude-sonnet-5" in rows, rows
    assert not any(_OLD_WORDING in row for row in rows), rows


def test_the_row_is_inert_same_as_before():
    """Tier 2: unchanged behaviour check — the prepended row's command
    stays empty (informational only), only the label text moved."""
    entries = _model_pane_entries([], "claude-sonnet-5")
    row, cmd = entries[0]
    assert row.startswith(_NEW_WORDING)
    assert cmd == ""


def test_a_matching_active_class_shows_no_prepended_row_at_all():
    """Tier 2: accept-side — when `active` genuinely IS one of `classes`,
    no informational row is prepended in either case; this issue only
    touches the mismatched-active row's own wording."""
    rows = model_pane_options(["fast", "careful"], "fast")
    assert not any("not in the class list below" in row for row in rows), rows
    assert rows == ["fast  · active", "careful"], rows
